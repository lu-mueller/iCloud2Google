#!/usr/bin/env python3
"""Synchronize iCloud CardDAV contacts into Google Contacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import ssl
import sys
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
import vobject
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv


ICLOUD_CARDDAV_URL = "https://contacts.icloud.com/"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/contacts"]
MANAGED_FIELDS = (
    "names",
    "nicknames",
    "emailAddresses",
    "phoneNumbers",
    "organizations",
    "addresses",
    "birthdays",
    "events",
    "urls",
    "calendarUrls",
    "externalIds",
    "biographies",
    "relations",
    "genders",
    "imClients",
    "locales",
    "locations",
    "sipAddresses",
    "userDefined",
    "clientData",
)
GOOGLE_FIELDS = ",".join((*MANAGED_FIELDS, "metadata", "photos"))
DAV_NS = "DAV:"
CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"
NS = {"d": DAV_NS, "c": CARDDAV_NS}
SYNC_MARKER_KEY = "icloud_carddav_sync_id"
SYNC_PHOTO_KEY = "icloud_carddav_photo_sha256"
IGNORED_VCARD_PROPERTIES = {
    "x-abadr",
    "x-ablabel",
    "x-abshowas",
    "iban",
    "x-iban",
}
TIMEOUT = (15, 60)
GOOGLE_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
GOOGLE_READ_RETRIES = 3
GOOGLE_CREATE_RETRY_DELAYS = (5, 15, 30)
GOOGLE_BATCH_DELETE_SIZE = 500
GOOGLE_BATCH_CREATE_SIZE = 200
FAILURE_EMAIL_DEFAULT = "lu_mueller@icloud.com"
ICLOUD_SMTP_HOST = "smtp.mail.me.com"
ICLOUD_SMTP_PORT = 587

LOGGER = logging.getLogger(__name__)


def _xml(tag: str) -> str:
    namespace, name = tag.split(":")
    return f"{{{NS[namespace]}}}{name}"


def _propfind_body(properties: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        f"<d:prop>{properties}</d:prop></d:propfind>"
    )


def _response_properties(root: ET.Element) -> list[tuple[str, ET.Element]]:
    results: list[tuple[str, ET.Element]] = []
    for response in root.findall("d:response", NS):
        href = response.findtext("d:href", default="", namespaces=NS)
        for propstat in response.findall("d:propstat", NS):
            status = propstat.findtext("d:status", default="", namespaces=NS)
            if " 2" not in status:
                continue
            properties = propstat.find("d:prop", NS)
            if properties is not None:
                results.append((href, properties))
    return results


class ICloudCardDAV:
    def __init__(self, username: str, app_password: str) -> None:
        self.session = requests.Session()
        self.session.auth = (username, app_password)
        self.session.headers["User-Agent"] = "icloud-to-google-contacts/1.0"

    def _request(
        self,
        method: str,
        url: str,
        body: str,
        *,
        depth: str,
    ) -> requests.Response:
        response = self.session.request(
            method,
            url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/xml; charset=utf-8",
                "Depth": depth,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response

    def fetch_vcards(self) -> list[tuple[str, str]]:
        initial = self._request(
            "PROPFIND",
            ICLOUD_CARDDAV_URL,
            _propfind_body("<d:current-user-principal/>"),
            depth="0",
        )
        initial_root = ET.fromstring(initial.content)
        principal_href = next(
            (
                node.text
                for node in initial_root.findall(
                    ".//d:current-user-principal/d:href", NS
                )
                if node.text
            ),
            None,
        )
        if not principal_href:
            raise RuntimeError("iCloud lieferte keinen CardDAV-Benutzerprinzipal.")
        principal_url = urljoin(initial.url, principal_href)

        principal = self._request(
            "PROPFIND",
            principal_url,
            _propfind_body("<c:addressbook-home-set/>"),
            depth="0",
        )
        principal_root = ET.fromstring(principal.content)
        home_href = next(
            (
                node.text
                for node in principal_root.findall(
                    ".//c:addressbook-home-set/d:href", NS
                )
                if node.text
            ),
            None,
        )
        if not home_href:
            raise RuntimeError("iCloud lieferte kein Adressbuch-Verzeichnis.")
        home_url = urljoin(principal.url, home_href)

        home = self._request(
            "PROPFIND",
            home_url,
            _propfind_body("<d:resourcetype/>"),
            depth="1",
        )
        home_root = ET.fromstring(home.content)
        addressbooks: list[str] = []
        for href, properties in _response_properties(home_root):
            resource_type = properties.find("d:resourcetype", NS)
            if resource_type is not None and resource_type.find("c:addressbook", NS) is not None:
                addressbooks.append(urljoin(home.url, href))
        if not addressbooks:
            raise RuntimeError("Im iCloud-Konto wurden keine Adressbücher gefunden.")

        query = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:addressbook-query xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:carddav">'
            "<d:prop><d:getetag/><c:address-data/></d:prop>"
            "</c:addressbook-query>"
        )
        cards: list[tuple[str, str]] = []
        for addressbook_url in addressbooks:
            response = self._request(
                "REPORT", addressbook_url, query, depth="1"
            )
            root = ET.fromstring(response.content)
            for href, properties in _response_properties(root):
                card_data = properties.findtext("c:address-data", namespaces=NS)
                if card_data and card_data.strip():
                    cards.append((href, card_data))
        return cards


def _values(card: Any, property_name: str) -> list[Any]:
    return card.contents.get(property_name.lower(), [])


def _property_text(properties: list[Any]) -> str:
    for prop in properties:
        value = getattr(prop, "value", None)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _types(prop: Any) -> list[str]:
    raw_types = getattr(prop, "params", {}).get("TYPE", [])
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    return [str(value).lower() for value in raw_types]


def _label_fields(prop: Any, allowed: set[str]) -> dict[str, str]:
    types = _types(prop)
    type_aliases = {"cell": "mobile", "fax": "otherfax"}
    google_type_names = {
        "homefax": "homeFax",
        "workfax": "workFax",
        "otherfax": "otherFax",
        "workmobile": "workMobile",
    }
    for candidate in types:
        normalized = type_aliases.get(candidate.lower(), candidate.lower())
        if normalized in allowed:
            return {"type": google_type_names.get(normalized, normalized)}
    if types:
        return {"type": types[0]}
    return {"type": "other"}


def _preferred(prop: Any) -> dict[str, dict[str, bool]]:
    params = getattr(prop, "params", {})
    preference = params.get("PREF")
    preferences = preference if isinstance(preference, (list, tuple)) else [preference]
    if (
        "1" in {str(value) for value in preferences if value is not None}
        or "pref" in _types(prop)
    ):
        return {"metadata": {"sourcePrimary": True}}
    return {}


def _birth_date(value: Any) -> dict[str, int] | None:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return {"year": value.year, "month": value.month, "day": value.day}
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, pattern).date()
            return {"year": parsed.year, "month": parsed.month, "day": parsed.day}
        except ValueError:
            pass
    for pattern in ("--%m%d", "--%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern).date()
            return {"month": parsed.month, "day": parsed.day}
        except ValueError:
            pass
    return None


def _scalar_text(value: Any) -> str:
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)) and all(
        isinstance(part, (str, int, float)) for part in value
    ):
        return ", ".join(str(part).strip() for part in value if str(part).strip())
    return ""


def vcard_to_google_person(vcard_data: str) -> dict[str, Any]:
    card = vobject.readOne(vcard_data)
    person: dict[str, Any] = {}

    name_properties = _values(card, "n")
    names: list[dict[str, str]] = []
    if name_properties:
        value = name_properties[0].value
        name: dict[str, str] = {}
        for source, target in (
            ("given", "givenName"),
            ("family", "familyName"),
            ("additional", "middleName"),
            ("prefix", "honorificPrefix"),
            ("suffix", "honorificSuffix"),
        ):
            part = getattr(value, source, "")
            if part:
                name[target] = str(part).strip()
        if name:
            names.append(name)
    if not names:
        formatted_name = _property_text(_values(card, "fn"))
        if formatted_name:
            names.append({"unstructuredName": formatted_name})
    if names:
        person["names"] = names

    nicknames = [
        {"value": text}
        for text in (_scalar_text(prop.value) for prop in _values(card, "nickname"))
        if text
    ]
    if nicknames:
        person["nicknames"] = nicknames

    emails = []
    for prop in _values(card, "email"):
        value = str(prop.value).strip()
        if value:
            emails.append(
                {
                    "value": value,
                    **_label_fields(prop, {"home", "work", "other"}),
                    **_preferred(prop),
                }
            )
    if emails:
        person["emailAddresses"] = emails

    phones = []
    for prop in _values(card, "tel"):
        value = str(prop.value).strip()
        if value:
            phones.append(
                {
                    "value": value,
                    **_label_fields(
                        prop,
                        {
                            "home",
                            "work",
                            "mobile",
                            "homefax",
                            "workfax",
                            "otherfax",
                            "pager",
                            "workmobile",
                            "callback",
                            "assistant",
                            "mms",
                            "other",
                        },
                    ),
                    **_preferred(prop),
                }
            )
    if phones:
        person["phoneNumbers"] = phones

    organizations = []
    for prop in _values(card, "org"):
        parts = prop.value if isinstance(prop.value, (list, tuple)) else [prop.value]
        parts = [str(part).strip() for part in parts if str(part).strip()]
        if parts:
            organization: dict[str, str] = {"name": parts[0]}
            if len(parts) > 1:
                organization["department"] = parts[1]
            if len(parts) > 2:
                organization["jobDescription"] = ", ".join(parts[2:])
            organizations.append(organization)
    for prop in _values(card, "title"):
        title = str(prop.value).strip()
        if title:
            if not organizations:
                organizations.append({})
            organizations[0]["title"] = title
            break
    for prop in _values(card, "role"):
        role = _scalar_text(prop.value)
        if role:
            if not organizations:
                organizations.append({})
            organizations[0]["jobDescription"] = role
            break
    if organizations:
        person["organizations"] = organizations

    addresses = []
    address_fields = (
        ("box", "poBox"),
        ("extended", "extendedAddress"),
        ("street", "streetAddress"),
        ("city", "city"),
        ("region", "region"),
        ("code", "postalCode"),
        ("country", "country"),
    )
    labels = {
        tuple(sorted(_types(prop))): _scalar_text(prop.value)
        for prop in _values(card, "label")
        if _scalar_text(prop.value)
    }
    for prop in _values(card, "adr"):
        value = prop.value
        address: dict[str, Any] = {
            **_label_fields(prop, {"home", "work", "other"}),
            **_preferred(prop),
        }
        for source, target in address_fields:
            part = getattr(value, source, "")
            if part:
                address[target] = str(part).strip()
        label = labels.get(tuple(sorted(_types(prop))))
        if label:
            address["formattedValue"] = label
        if len(address) > 1:
            addresses.append(address)
    if addresses:
        person["addresses"] = addresses

    birthdays = []
    for prop in _values(card, "bday"):
        birthday = _birth_date(prop.value)
        if birthday:
            birthdays.append({"date": birthday})
    if birthdays:
        person["birthdays"] = birthdays

    events = []
    for prop in _values(card, "anniversary"):
        event_date = _birth_date(prop.value)
        if event_date:
            events.append({"date": event_date, "type": "anniversary"})
    if events:
        person["events"] = events

    urls = []
    for prop in _values(card, "url"):
        value = str(prop.value).strip()
        if value:
            urls.append(
                {
                    "value": value,
                    **_label_fields(prop, {"home", "work", "blog", "profile", "other"}),
                    **_preferred(prop),
                }
            )
    for prop in _values(card, "x-socialprofile"):
        value = _scalar_text(prop.value)
        if value:
            urls.append({"value": value, "type": "profile"})
    if urls:
        person["urls"] = urls

    calendar_urls = []
    for prop in _values(card, "caluri"):
        value = _scalar_text(prop.value)
        if value:
            calendar_urls.append(
                {"url": value, **_label_fields(prop, {"home", "work", "other"})}
            )
    if calendar_urls:
        person["calendarUrls"] = calendar_urls

    notes = []
    for prop in _values(card, "note"):
        value = str(prop.value).strip()
        if value:
            notes.append(value)
    if notes:
        person["biographies"] = [{"value": "\n".join(notes)}]

    relations = []
    for prop in _values(card, "related"):
        value = _scalar_text(prop.value)
        if not value:
            continue
        relation_type = next(iter(_types(prop)), "other")
        relations.append({"person": value, "type": relation_type})
    if relations:
        person["relations"] = relations

    genders = []
    for prop in _values(card, "sex"):
        value = getattr(prop.value, "sex", prop.value)
        gender = {
            "M": "male",
            "F": "female",
            "O": "other",
            "N": "other",
            "U": "unspecified",
        }.get(str(value).upper(), "unspecified")
        genders.append({"value": gender})
    if genders:
        person["genders"] = genders

    im_clients = []
    for prop in _values(card, "impp"):
        value = _scalar_text(prop.value)
        if value:
            parsed = urlsplit(value)
            if parsed.scheme.lower() == "sip":
                continue
            username = parsed.path or parsed.netloc
            im_clients.append(
                {
                    "protocol": parsed.scheme or "other",
                    "username": username,
                    **_label_fields(prop, {"home", "work", "other"}),
                }
            )
    if im_clients:
        person["imClients"] = im_clients

    sip_addresses = []
    for prop in _values(card, "impp"):
        value = _scalar_text(prop.value)
        if value and urlsplit(value).scheme.lower() == "sip":
            sip_addresses.append(
                {
                    "value": value,
                    **_label_fields(prop, {"home", "work", "other"}),
                    **_preferred(prop),
                }
            )
    if sip_addresses:
        person["sipAddresses"] = sip_addresses

    locations = []
    for prop in _values(card, "geo"):
        value = _scalar_text(prop.value)
        if value:
            locations.append({"value": value, "type": "geo"})
    if locations:
        person["locations"] = locations

    custom_fields = []
    mapped_properties = {
        "n",
        "fn",
        "nickname",
        "email",
        "tel",
        "org",
        "title",
        "role",
        "adr",
        "label",
        "bday",
        "anniversary",
        "url",
        "x-socialprofile",
        "photo",
        "caluri",
        "note",
        "related",
        "sex",
        "impp",
        "geo",
        "lang",
        "uid",
        "x-abadr",
        "version",
        "prodid",
        "rev",
        "begin",
        "end",
    }
    for property_name, properties in card.contents.items():
        normalized_property_name = property_name.casefold()
        if (
            normalized_property_name in mapped_properties
            or normalized_property_name in IGNORED_VCARD_PROPERTIES
            or "iban" in normalized_property_name
        ):
            continue
        for prop in properties:
            value = _scalar_text(prop.value)
            if value:
                custom_fields.append(
                    {"key": f"iCloud: {property_name.upper()}", "value": value}
                )
    if custom_fields:
        person["userDefined"] = custom_fields

    return person


def _identity_marker(vcard_data: str, person: dict[str, Any]) -> str:
    card = vobject.readOne(vcard_data)
    uid = _property_text(_values(card, "uid"))
    if uid:
        uid_hash = hashlib.sha256(uid.encode("utf-8")).hexdigest()
        return f"icloud-uid-sha256:{uid_hash}"
    fingerprint = hashlib.sha256(
        json.dumps(person, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"hash:{fingerprint}"


def _legacy_identity_marker(vcard_data: str) -> str | None:
    card = vobject.readOne(vcard_data)
    uid = _property_text(_values(card, "uid"))
    return f"uid:{uid}" if uid else None


def _email_keys(person: dict[str, Any]) -> set[str]:
    return {
        str(item.get("value", "")).strip().casefold()
        for item in person.get("emailAddresses", [])
        if item.get("value")
    }


def _phone_keys(person: dict[str, Any]) -> set[str]:
    return {
        "".join(character for character in str(item.get("value", "")) if character.isdigit())
        for item in person.get("phoneNumbers", [])
        if sum(character.isdigit() for character in str(item.get("value", ""))) >= 7
    }


def _marker_values(person: dict[str, Any]) -> set[str]:
    return {
        str(item.get("value", ""))
        for item in [
            *person.get("userDefined", []),
            *person.get("clientData", []),
        ]
        if item.get("key") == SYNC_MARKER_KEY and item.get("value")
    }


def find_matches(
    source: dict[str, Any],
    marker: str,
    existing: list[dict[str, Any]],
    legacy_markers: set[str] | None = None,
) -> list[dict[str, Any]]:
    source_emails = _email_keys(source)
    source_phones = _phone_keys(source)
    markers = {marker, *(legacy_markers or set())}
    matches = []
    for person in existing:
        same_marker = bool(markers & _marker_values(person))
        same_email = bool(source_emails & _email_keys(person))
        same_phone = bool(source_phones & _phone_keys(person))
        if same_marker or same_email or same_phone:
            matches.append(person)
    return matches


def _manual_google_authorization(flow: InstalledAppFlow) -> Credentials:
    flow.redirect_uri = "http://localhost"
    authorization_url, expected_state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    print(
        "Öffne diese URL in einem Browser (auch auf einem anderen Gerät):\n"
        f"{authorization_url}\n"
        "Nach der Anmeldung wird die localhost-Seite eventuell nicht "
        "erreichbar sein. Kopiere dann die vollständige URL aus der "
        "Adresszeile und füge sie hier ein."
    )
    try:
        authorization_response = input("Weiterleitungs-URL: ").strip()
    except EOFError as error:
        raise RuntimeError("Keine OAuth-Weiterleitungs-URL eingegeben.") from error

    redirect = urlsplit(authorization_response)
    if redirect.scheme != "http" or redirect.hostname != "localhost":
        raise ValueError(
            "Die OAuth-Weiterleitungs-URL muss auf http://localhost zeigen."
        )
    parameters = parse_qs(redirect.query, keep_blank_values=True)
    if parameters.get("state") != [expected_state]:
        raise ValueError(
            "Der OAuth-Status in der Weiterleitungs-URL stimmt nicht "
            "mit der Anmeldung überein. Starte die Anmeldung erneut."
        )
    if parameters.get("error"):
        raise RuntimeError(
            f"Google hat die OAuth-Anmeldung abgelehnt: {parameters['error'][0]}"
        )
    authorization_codes = parameters.get("code", [])
    if len(authorization_codes) != 1 or not authorization_codes[0]:
        raise ValueError(
            "In der OAuth-Weiterleitungs-URL fehlt ein eindeutiger "
            "Autorisierungscode."
        )
    flow.fetch_token(code=authorization_codes[0])
    return flow.credentials


def _google_credentials(
    client_file: Path,
    token_file: Path,
    client_id: str,
    client_secret: str,
    *,
    manual_auth: bool = False,
) -> Credentials:
    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_file), GOOGLE_SCOPES
        )
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if client_id and client_secret:
            config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(config, GOOGLE_SCOPES)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_file), GOOGLE_SCOPES
            )
        if manual_auth:
            credentials = _manual_google_authorization(flow)
        else:
            try:
                credentials = flow.run_local_server(port=0)
            except webbrowser.Error:
                LOGGER.warning(
                    "Kein lokaler Browser verfügbar; wechsle zur manuellen "
                    "OAuth-Anmeldung."
                )
                credentials = _manual_google_authorization(flow)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def _send_failure_email(error_message: str) -> None:
    username = os.environ.get("ICLOUD_USERNAME", "").strip()
    app_password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
    recipient = os.environ.get("FAILURE_EMAIL_TO", FAILURE_EMAIL_DEFAULT).strip()
    if not username or not app_password:
        raise RuntimeError(
            "Fehler-Mail nicht gesendet: ICLOUD_USERNAME oder "
            "ICLOUD_APP_PASSWORD fehlt."
        )
    if not recipient:
        raise RuntimeError("Fehler-Mail nicht gesendet: FAILURE_EMAIL_TO ist leer.")

    message = EmailMessage()
    message["Subject"] = "Fehler bei iCloud-Google-Kontaktsynchronisierung"
    message["From"] = username
    message["To"] = recipient
    message.set_content(
        "Die Kontaktsynchronisierung ist fehlgeschlagen.\n\n"
        f"iCloud-Konto: {username}\n"
        f"Zeit: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"Fehler: {error_message}\n\n"
        "Aus Sicherheitsgründen enthält diese Nachricht keine Passwörter "
        "oder Kontaktdaten."
    )

    with smtplib.SMTP(
        ICLOUD_SMTP_HOST, ICLOUD_SMTP_PORT, timeout=30
    ) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(username, app_password)
        smtp.send_message(message)
    LOGGER.info("Fehler-Mail an %s gesendet.", recipient)


def _notify_failure(error_message: str) -> None:
    try:
        _send_failure_email(error_message)
    except (OSError, RuntimeError, ValueError, smtplib.SMTPException) as mail_error:
        LOGGER.error("Fehler-Mail konnte nicht gesendet werden: %s", mail_error)


def _google_contacts(service: Any) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    page_token = None
    while True:
        people_api = getattr(service, "people")()
        connections_api = getattr(people_api, "connections")()
        request = connections_api.list(
            resourceName="people/me",
            pageSize=1000,
            personFields=GOOGLE_FIELDS,
            pageToken=page_token,
        )
        response = _execute_google(request, "Google-Kontaktliste abrufen")
        contacts.extend(response.get("connections", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return contacts


def _is_owned_google_contact(person: dict[str, Any]) -> bool:
    return any(
        source.get("type") == "CONTACT"
        for source in person.get("metadata", {}).get("sources", [])
    )


def _delete_google_contacts(service: Any, contacts: list[dict[str, Any]]) -> int:
    resource_names = [
        person["resourceName"]
        for person in contacts
        if _is_owned_google_contact(person) and person.get("resourceName")
    ]
    people_api = getattr(service, "people")()
    for offset in range(0, len(resource_names), GOOGLE_BATCH_DELETE_SIZE):
        batch = resource_names[offset : offset + GOOGLE_BATCH_DELETE_SIZE]
        LOGGER.info(
            "Lösche Google-Kontakte %d–%d von %d.",
            offset + 1,
            offset + len(batch),
            len(resource_names),
        )
        request = people_api.batchDeleteContacts(
            body={"resourceNames": batch}
        )
        try:
            _execute_google(request, "Google-Kontakte löschen", retries=0)
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status in GOOGLE_RETRYABLE_STATUS:
                raise RuntimeError(
                    f"Google antwortete beim Löschen mit HTTP {status}. "
                    "Der Löschstatus kann teilweise sein. Starte später "
                    "erneut mit --replace-all; vor dem Import wird die "
                    "Kontaktliste erneut geprüft."
                ) from error
            raise
    return len(resource_names)


def _create_google_contact(
    service: Any, person: dict[str, Any]
) -> dict[str, Any]:
    people_api = getattr(service, "people")()
    request = people_api.createContact(body=person)
    return _execute_google(request, "Google-Kontakt anlegen", retries=0)


def _batch_create_google_contacts(
    service: Any, contacts: list[dict[str, Any]]
) -> int:
    people_api = getattr(service, "people")()
    created_count = 0

    for offset in range(0, len(contacts), GOOGLE_BATCH_CREATE_SIZE):
        batch = contacts[offset : offset + GOOGLE_BATCH_CREATE_SIZE]
        pending = batch
        confirmed_count = 0
        LOGGER.info(
            "Importiere Kontakte %d–%d von %d in einem Batch.",
            offset + 1,
            offset + len(batch),
            len(contacts),
        )

        for attempt, delay in enumerate(GOOGLE_CREATE_RETRY_DELAYS):
            request = people_api.batchCreateContacts(
                body={
                    "contacts": [
                        {"contactPerson": person} for person in pending
                    ],
                    "readMask": GOOGLE_FIELDS,
                    "sources": ["READ_SOURCE_TYPE_CONTACT"],
                }
            )
            try:
                response = _execute_google(
                    request, "Google-Kontakte im Batch anlegen", retries=0
                )
            except HttpError as error:
                status = getattr(error.resp, "status", None)
                if status not in GOOGLE_RETRYABLE_STATUS:
                    raise
                LOGGER.warning(
                    "Batch-Anlage antwortete mit HTTP %s. Warte %d s und "
                    "prüfe erst, welche Kontakte bereits gespeichert wurden.",
                    status,
                    delay,
                )
                time.sleep(delay)
                existing = _google_contacts(service)
                existing_markers = {
                    marker
                    for person in existing
                    for marker in _marker_values(person)
                }
                found = [
                    person
                    for person in pending
                    if _marker_values(person) & existing_markers
                ]
                confirmed_count += len(found)
                found_ids = {
                    next(iter(_marker_values(person) & existing_markers))
                    for person in found
                }
                pending = [
                    person
                    for person in pending
                    if not (_marker_values(person) & found_ids)
                ]
                if not pending:
                    break
                if attempt == len(GOOGLE_CREATE_RETRY_DELAYS) - 1:
                    raise RuntimeError(
                        f"Google bestätigte nur {confirmed_count} von "
                        f"{len(batch)} Kontakten aus dem Batch. Starte später "
                        "erneut mit --replace-all; zuerst werden Kontakte "
                        "erneut exportiert und Google-Kontakte neu abgeglichen."
                    ) from error
                LOGGER.warning(
                    "%d von %d Kontakten bestätigt; wiederhole den Restbatch "
                    "(%d Kontakte).",
                    confirmed_count,
                    len(batch),
                    len(pending),
                )
                continue

            created_people = response.get("createdPeople", [])
            if len(created_people) != len(pending):
                raise RuntimeError(
                    f"Google bestätigte {len(created_people)} von "
                    f"{len(pending)} Kontakten im Batch."
                )
            confirmed_count += len(pending)
            break
        else:
            raise RuntimeError("Google-Batchimport unerwartet beendet.")

        created_count += confirmed_count
        LOGGER.info(
            "%d von %d Kontakten im Batch bestätigt.",
            confirmed_count,
            len(batch),
        )

    return created_count


def _get_google_contact(service: Any, resource_name: str) -> dict[str, Any]:
    people_api = getattr(service, "people")()
    request = people_api.get(
        resourceName=resource_name,
        personFields=GOOGLE_FIELDS,
    )
    return _execute_google(request, "Google-Kontaktstatus abrufen")


def _execute_google(
    request: Any, operation: str, *, retries: int = GOOGLE_READ_RETRIES
) -> Any:
    for attempt in range(retries + 1):
        try:
            return request.execute()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status not in GOOGLE_RETRYABLE_STATUS or attempt >= retries:
                raise
            delay = min(2**attempt, 30)
            LOGGER.warning(
                "%s: Google antwortet vorübergehend mit HTTP %s; "
                "neuer Versuch %d/%d in %d s.",
                operation,
                status,
                attempt + 1,
                retries,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError(f"{operation}: Wiederholungsversuche unerwartet beendet.")


def _google_update_body(
    existing: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    resource_name = existing.get("resourceName")
    etag = existing.get("etag")
    if not resource_name or not etag:
        raise RuntimeError(
            "Google-Kontakt kann ohne resourceName und etag nicht aktualisiert werden."
        )
    metadata = existing.get("metadata")
    if not metadata or not any(
        source.get("type") == "CONTACT"
        for source in metadata.get("sources", [])
    ):
        raise RuntimeError(
            "Google-Kontakt enthält keine CONTACT-Quellmetadaten für das Update."
        )

    custom_fields = [
        item
        for item in existing.get("userDefined", [])
        if item.get("key") not in {SYNC_MARKER_KEY, SYNC_PHOTO_KEY}
        and not str(item.get("key", "")).startswith("iCloud: ")
    ]
    custom_fields.extend(
        item
        for item in source.get("userDefined", [])
        if str(item.get("key", "")).startswith("iCloud: ")
    )
    client_data = [
        item
        for item in existing.get("clientData", [])
        if item.get("key") != SYNC_MARKER_KEY
    ]
    client_data.extend(
        item
        for item in source.get("clientData", [])
        if item.get("key") == SYNC_MARKER_KEY
    )
    body: dict[str, Any] = {
        "resourceName": resource_name,
        "etag": etag,
        "metadata": metadata,
    }
    for field in MANAGED_FIELDS:
        if field == "userDefined":
            body[field] = custom_fields
        elif field == "clientData":
            body[field] = client_data
        else:
            body[field] = source.get(field, [])
    return body


def _update_google_contact(
    service: Any, existing: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    people_api = getattr(service, "people")()
    contact = existing
    for attempt, delay in enumerate(GOOGLE_CREATE_RETRY_DELAYS):
        body = _google_update_body(contact, source)
        request = people_api.updateContact(
            resourceName=body["resourceName"],
            updatePersonFields=",".join(MANAGED_FIELDS),
            body=body,
        )
        try:
            return _execute_google(
                request, "Google-Kontakt aktualisieren", retries=0
            )
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status not in GOOGLE_RETRYABLE_STATUS:
                raise
            LOGGER.warning(
                "Google antwortete beim Aktualisieren mit HTTP %s. "
                "Warte %d s und lese den aktuellen Kontakt vor dem erneuten Versuch.",
                status,
                delay,
            )
            time.sleep(delay)
            contact = _get_google_contact(service, body["resourceName"])
            if attempt == len(GOOGLE_CREATE_RETRY_DELAYS) - 1:
                raise RuntimeError(
                    f"Google-Kontakt {body['resourceName']} konnte nach "
                    f"mehreren Versuchen nicht aktualisiert werden (HTTP {status})."
                ) from error
    raise RuntimeError("Google-Kontaktaktualisierung unerwartet beendet.")


def _remove_synced_google_photo(service: Any, contact: dict[str, Any]) -> dict[str, Any]:
    resource_name = contact.get("resourceName")
    if not resource_name:
        raise RuntimeError("Google-Kontakt enthält keinen resourceName für das Foto.")

    marker = next(
        (
            item.get("value")
            for item in contact.get("userDefined", [])
            if item.get("key") == SYNC_PHOTO_KEY
        ),
        None,
    )
    if not marker or not contact.get("photos"):
        return contact

    people_api = getattr(service, "people")()
    for attempt, delay in enumerate(GOOGLE_CREATE_RETRY_DELAYS):
        request = people_api.deleteContactPhoto(
            resourceName=resource_name,
            personFields="metadata,photos,userDefined",
        )
        try:
            result = _execute_google(
                request, "Synchronisiertes Kontaktfoto entfernen", retries=0
            )
            updated = result.get("person")
            if not updated:
                raise RuntimeError(
                    "Google hat nach dem Entfernen des Fotos keinen Kontakt geliefert."
                )
            LOGGER.info("Synchronisiertes Kontaktfoto entfernt.")
            return {**contact, **updated}
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status not in GOOGLE_RETRYABLE_STATUS:
                raise
            LOGGER.warning(
                "Google antwortete beim Entfernen des Fotos mit HTTP %s. "
                "Warte %d s und prüfe den aktuellen Fotostatus.",
                status,
                delay,
            )
            time.sleep(delay)
            contact = _get_google_contact(service, resource_name)
            if not contact.get("photos"):
                LOGGER.info("Kontaktfoto war bereits entfernt.")
                return contact
            if attempt == len(GOOGLE_CREATE_RETRY_DELAYS) - 1:
                raise RuntimeError(
                    f"Kontaktfoto von {resource_name} konnte nach mehreren "
                    f"Versuchen nicht entfernt werden (HTTP {status})."
                ) from error
    raise RuntimeError("Entfernen des Google-Kontaktfotos unerwartet beendet.")


def _create_with_recovery(
    service: Any,
    person: dict[str, Any],
    marker: str,
    legacy_markers: set[str],
    *,
    index: int,
    total: int,
) -> dict[str, Any]:
    markers = {marker, *legacy_markers}
    for attempt, delay in enumerate(GOOGLE_CREATE_RETRY_DELAYS):
        try:
            return _create_google_contact(service, person)
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status not in GOOGLE_RETRYABLE_STATUS:
                raise

            LOGGER.warning(
                "[%d/%d] Google antwortete beim Anlegen mit HTTP %s. "
                "Warte %d s und prüfe, ob der Kontakt trotzdem gespeichert wurde.",
                index,
                total,
                status,
                delay,
            )
            time.sleep(delay)
            refreshed = _google_contacts(service)
            result = next(
                (
                    contact
                    for contact in refreshed
                    if markers & _marker_values(contact)
                ),
                None,
            )
            if result is not None:
                LOGGER.info(
                    "[%d/%d] Kontakt nach HTTP %s in Google gefunden; "
                    "kein weiterer Kontakt wird angelegt.",
                    index,
                    total,
                    status,
                )
                return result

            if attempt == len(GOOGLE_CREATE_RETRY_DELAYS) - 1:
                raise RuntimeError(
                    f"Google bestätigte das Anlegen von Kontakt {index} "
                    f"nicht (HTTP {status}) nach mehreren Prüfungen. "
                    "Bitte den Kontakt in Google prüfen, bevor du den Lauf "
                    "erneut startest."
                ) from error
            LOGGER.warning(
                "[%d/%d] Kein Kontakt mit der iCloud-Kennung gefunden; "
                "wiederhole den Anlegeversuch (%d/%d).",
                index,
                total,
                attempt + 1,
                len(GOOGLE_CREATE_RETRY_DELAYS),
            )
    raise RuntimeError("Google-Anlage unerwartet beendet.")


def sync(
    *, dry_run: bool, replace_all: bool = False, manual_oauth: bool = False
) -> tuple[int, int, int]:
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    username = os.environ.get("ICLOUD_USERNAME", "").strip()
    app_password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
    if not username or not app_password:
        raise RuntimeError(
            "Bitte ICLOUD_USERNAME und ICLOUD_APP_PASSWORD als "
            "Umgebungsvariablen setzen."
        )

    client_file = Path(os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "credentials.json"))
    token_file = Path(os.environ.get("GOOGLE_TOKEN_FILE", "token.json"))
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if bool(client_id) != bool(client_secret):
        raise RuntimeError(
            "GOOGLE_CLIENT_ID und GOOGLE_CLIENT_SECRET müssen gemeinsam gesetzt sein."
        )
    if not (client_id and client_secret) and not client_file.is_file():
        raise FileNotFoundError(
            "Google-OAuth-Zugangsdaten fehlen. Setze GOOGLE_CLIENT_ID und "
            "GOOGLE_CLIENT_SECRET oder lege die OAuth-JSON-Datei unter "
            f"{client_file} ab."
        )

    LOGGER.info("Lese Kontaktkarten aus iCloud.")
    cards = ICloudCardDAV(username, app_password).fetch_vcards()
    LOGGER.info("%d Kontaktkarten aus iCloud geladen.", len(cards))
    sources: list[tuple[dict[str, Any], str, set[str]]] = []
    for _, vcard_data in cards:
        person = vcard_to_google_person(vcard_data)
        marker = _identity_marker(vcard_data, person)
        legacy_marker = _legacy_identity_marker(vcard_data)
        person.setdefault("clientData", []).append(
            {"key": SYNC_MARKER_KEY, "value": marker}
        )
        legacy_markers = {legacy_marker} if legacy_marker else set()
        sources.append((person, marker, legacy_markers))
    if replace_all and not sources:
        raise RuntimeError(
            "Der iCloud-Export ist leer; Google-Kontakte werden nicht gelöscht."
        )

    credentials = _google_credentials(
        client_file,
        token_file,
        client_id,
        client_secret,
        manual_auth=manual_oauth,
    )
    service = build("people", "v1", credentials=credentials, cache_discovery=False)
    LOGGER.info("Lese vorhandene Google-Kontakte.")
    existing = _google_contacts(service)
    LOGGER.info("%d Google-Kontakte geladen.", len(existing))
    if replace_all:
        to_delete = [
            person for person in existing if _is_owned_google_contact(person)
        ]
        if dry_run:
            LOGGER.info(
                "Dry-Run: Würde %d Google-Kontakte löschen und "
                "%d iCloud-Kontakte importieren.",
                len(to_delete),
                len(sources),
            )
            return len(sources), 0, 0

        deleted = _delete_google_contacts(service, to_delete)
        LOGGER.info(
            "%d Google-Kontakte gelöscht; starte den vollständigen iCloud-Import.",
            deleted,
        )
        created = _batch_create_google_contacts(
            service, [person for person, _, _ in sources]
        )
        LOGGER.info(
            "Synchronisierung abgeschlossen: %d Kontakte im Batch importiert.",
            created,
        )
        return created, 0, 0

    created = updated = ambiguous = 0

    for index, (person, marker, legacy_markers) in enumerate(sources, start=1):
        LOGGER.info("[%d/%d] Synchronisiere Kontakt.", index, len(sources))
        matches = find_matches(person, marker, existing, legacy_markers)
        if len(matches) > 1:
            ambiguous += 1
            LOGGER.warning(
                "[%d/%d] Mehrere Google-Treffer; Kontakt unverändert gelassen.",
                index,
                len(sources),
            )
            continue
        if matches:
            match = matches[0]
            if dry_run:
                body = _google_update_body(match, person)
                updated_person = {
                    **match,
                    **{
                        field: body[field]
                        for field in MANAGED_FIELDS
                    },
                }
            else:
                if any(
                    item.get("key") == SYNC_PHOTO_KEY
                    for item in match.get("userDefined", [])
                ) and match.get("photos"):
                    LOGGER.info(
                        "[%d/%d] Entferne das zuvor synchronisierte Kontaktfoto.",
                        index,
                        len(sources),
                    )
                match = _remove_synced_google_photo(service, match)
                LOGGER.info("[%d/%d] Aktualisiere Google-Kontaktfelder.", index, len(sources))
                updated_person = {
                    **match,
                    **_update_google_contact(service, match, person),
                }
            existing[existing.index(match)] = updated_person
            updated += 1
            LOGGER.info(
                "[%d/%d] Kontaktfelder aktualisiert%s.",
                index,
                len(sources),
                " (Dry-Run)" if dry_run else "",
            )
            continue
        if dry_run:
            created += 1
            LOGGER.info(
                "[%d/%d] Neuer Kontakt würde angelegt%s.",
                index,
                len(sources),
                " (Dry-Run)" if dry_run else "",
            )
        else:
            result = _create_with_recovery(
                service,
                person,
                marker,
                legacy_markers,
                index=index,
                total=len(sources),
            )
            existing.append(result)
            created += 1
            LOGGER.info("[%d/%d] Kontakt angelegt.", index, len(sources))
            continue
        existing.append(person)

    LOGGER.info(
        "Synchronisierung abgeschlossen: %d neu, %d aktualisiert, "
        "%d mehrdeutig übersprungen.",
        created,
        updated,
        ambiguous,
    )
    return created, updated, ambiguous


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    arguments = set(sys.argv[1:])
    dry_run = "--dry-run" in arguments
    replace_all = "--incremental" not in arguments
    manual_oauth = "--oauth-manual" in arguments
    test_email = "--test-email" in arguments
    unknown_arguments = [
        arg
        for arg in arguments
        if arg
        not in {
            "--dry-run",
            "--replace-all",
            "--incremental",
            "--oauth-manual",
            "--test-email",
        }
    ]
    if unknown_arguments:
        print(
            f"Unbekannte Argumente: {' '.join(unknown_arguments)}",
            file=sys.stderr,
        )
        return 2
    if "--incremental" in arguments and "--replace-all" in arguments:
        print("--incremental und --replace-all schließen sich aus.", file=sys.stderr)
        return 2
    if test_email and arguments != {"--test-email"}:
        print("--test-email kann nicht mit anderen Optionen kombiniert werden.", file=sys.stderr)
        return 2
    if test_email:
        try:
            _send_failure_email(
                "Testnachricht: Die Fehlerbenachrichtigung wurde erfolgreich getestet."
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            smtplib.SMTPException,
        ) as error:
            LOGGER.error("Test-Mail fehlgeschlagen: %s", error)
            return 1
        print("Test-Mail wurde erfolgreich gesendet.")
        return 0

    try:
        created, updated, ambiguous = sync(
            dry_run=dry_run,
            replace_all=replace_all,
            manual_oauth=manual_oauth,
        )
    except HttpError as error:
        status = getattr(error.resp, "status", "unbekannt")
        reason = getattr(error, "reason", "Google API-Fehler")
        error_message = f"Google HTTP {status}: {reason}"
        LOGGER.error("Synchronisierung fehlgeschlagen: %s", error_message)
        _notify_failure(error_message)
        return 1
    except (
        requests.RequestException,
        ET.ParseError,
        OSError,
        RuntimeError,
        ValueError,
        smtplib.SMTPException,
    ) as error:
        LOGGER.error("Synchronisierung fehlgeschlagen: %s", error)
        _notify_failure(str(error))
        return 1

    if dry_run:
        print(
            f"Würden angelegt: {created}; würden aktualisiert: {updated}; "
            f"mehrdeutige Treffer übersprungen: {ambiguous}."
        )
        print("Dry-Run: Es wurden keine Kontakte in Google geändert.")
    else:
        print(
            f"Angelegt: {created}; aktualisiert: {updated}; "
            f"mehrdeutige Treffer übersprungen: {ambiguous}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
