import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from googleapiclient.errors import HttpError

from icloud_to_google import (
    MANAGED_FIELDS,
    _batch_create_google_contacts,
    _create_google_contact,
    _create_with_recovery,
    _delete_google_contacts,
    _execute_google,
    _google_update_body,
    _identity_marker,
    _legacy_identity_marker,
    _remove_synced_google_photo,
    _google_credentials,
    _is_owned_google_contact,
    _send_failure_email,
    _update_google_contact,
    find_matches,
    main,
    sync,
    vcard_to_google_person,
)


VCARD = """\
BEGIN:VCARD
VERSION:3.0
UID:contact-123
N:Doe;Jane;Middle;Dr.;Jr.
FN:Dr. Jane Middle Doe
EMAIL;TYPE=WORK;PREF=1:jane@example.com
TEL;TYPE=CELL:+1-555-555-5555
ORG:Example Corp;Engineering
TITLE:Director
ADR;TYPE=HOME:PO Box 42;Suite 5;123 Main St;Anytown;CA;12345;USA
LABEL;TYPE=HOME:Suite 5\\, 123 Main St\\, Anytown\\, CA 12345\\, USA
BDAY:19900102
ANNIVERSARY:20180615
URL:https://example.com
NOTE:Example note
PHOTO;VALUE=URI:https://example.invalid/photo.jpg
NICKNAME:Janey
RELATED;TYPE=spouse:John Doe
SEX:F
IMPP;TYPE=HOME:xmpp:jane@example.com
IMPP:sip:jane@example.com
LANG:de-DE
CATEGORIES:friend,work
GEO:37.3;-122.0
X-ABADR:de
X-ABSHOWAS:COMPANY
X-ABLABEL:IBAN
IBAN:DE89370400440532013000
X-IBAN:DE89370400440532013000
X-CUSTOM:custom value
END:VCARD
"""
class ContactMappingTests(unittest.TestCase):
    def test_maps_common_vcard_fields(self) -> None:
        person = vcard_to_google_person(VCARD)

        self.assertEqual(person["names"][0]["givenName"], "Jane")
        self.assertEqual(person["names"][0]["familyName"], "Doe")
        self.assertEqual(person["emailAddresses"][0]["value"], "jane@example.com")
        self.assertTrue(person["emailAddresses"][0]["metadata"]["sourcePrimary"])
        self.assertEqual(person["phoneNumbers"][0]["value"], "+1-555-555-5555")
        self.assertEqual(person["organizations"][0]["title"], "Director")
        address = person["addresses"][0]
        self.assertEqual(address["poBox"], "PO Box 42")
        self.assertEqual(address["extendedAddress"], "Suite 5")
        self.assertEqual(address["streetAddress"], "123 Main St")
        self.assertEqual(address["city"], "Anytown")
        self.assertEqual(address["region"], "CA")
        self.assertEqual(address["postalCode"], "12345")
        self.assertEqual(address["country"], "USA")
        self.assertEqual(
            address["formattedValue"],
            "Suite 5, 123 Main St, Anytown, CA 12345, USA",
        )
        self.assertEqual(person["birthdays"][0]["date"]["year"], 1990)
        self.assertEqual(person["events"][0]["type"], "anniversary")
        self.assertEqual(person["urls"][0]["value"], "https://example.com")
        self.assertEqual(person["biographies"][0]["value"], "Example note")
        self.assertEqual(person["nicknames"][0]["value"], "Janey")
        self.assertEqual(person["relations"][0]["person"], "John Doe")
        self.assertEqual(person["genders"][0]["value"], "female")
        self.assertEqual(person["imClients"][0]["username"], "jane@example.com")
        self.assertEqual(person["sipAddresses"][0]["value"], "sip:jane@example.com")
        self.assertNotIn("locales", person)
        self.assertEqual(person["locations"][0]["value"], "37.3;-122.0")
        self.assertNotIn("externalIds", person)
        self.assertNotIn(
            "iCloud: PHOTO",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertNotIn(
            "iCloud: X-ABADR",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertNotIn(
            "iCloud: X-ABSHOWAS",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertNotIn(
            "iCloud: IBAN",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertNotIn(
            "iCloud: X-IBAN",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertNotIn(
            "iCloud: X-ABLABEL",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertNotIn(
            "iCloud: IBAN",
            {item["key"] for item in person.get("userDefined", [])},
        )
        self.assertIn(
            {"key": "iCloud: CATEGORIES", "value": "friend, work"},
            person["userDefined"],
        )
        self.assertIn(
            {"key": "iCloud: X-CUSTOM", "value": "custom value"},
            person["userDefined"],
        )

    def test_uid_is_hashed_for_new_marker_and_legacy_marker_still_matches(self) -> None:
        person = vcard_to_google_person(VCARD)
        marker = _identity_marker(VCARD, person)
        legacy_marker = _legacy_identity_marker(VCARD)
        if legacy_marker is None:
            self.fail("UID aus der Test-vCard wurde nicht erkannt.")
        existing = [
            {
                "userDefined": [
                    {
                        "key": "icloud_carddav_sync_id",
                        "value": legacy_marker,
                    }
                ]
            }
        ]

        self.assertTrue(marker.startswith("icloud-uid-sha256:"))
        self.assertNotIn("contact-123", marker)
        self.assertEqual(
            find_matches(person, marker, existing, {legacy_marker}), existing
        )

    def test_unknown_phone_label_uses_google_custom_type_field(self) -> None:
        person = vcard_to_google_person(
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "FN:Jane Doe\n"
            "TEL;TYPE=VOICE:555-0100\n"
            "END:VCARD\n"
        )

        self.assertEqual(person["phoneNumbers"][0]["type"], "voice")
        self.assertNotIn("customType", person["phoneNumbers"][0])

    def test_google_read_retries_temporary_server_errors(self) -> None:
        response = Mock(status=502)
        request = Mock()
        request.execute.side_effect = [
            HttpError(response, b"temporary failure"),
            HttpError(response, b"temporary failure"),
            {"connections": []},
        ]

        with patch("icloud_to_google.time.sleep") as sleep:
            result = _execute_google(request, "Kontaktliste abrufen")

        self.assertEqual(result, {"connections": []})
        self.assertEqual(request.execute.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_replace_mode_deletes_only_owned_google_contacts(self) -> None:
        service = Mock()
        owned = {
            "resourceName": "people/123",
            "metadata": {"sources": [{"type": "CONTACT"}]},
        }
        profile = {
            "resourceName": "people/profile",
            "metadata": {"sources": [{"type": "PROFILE"}]},
        }
        request = service.people.return_value.batchDeleteContacts.return_value
        request.execute.return_value = {}

        self.assertTrue(_is_owned_google_contact(owned))
        self.assertFalse(_is_owned_google_contact(profile))
        deleted = _delete_google_contacts(service, [owned, profile])

        self.assertEqual(deleted, 1)
        service.people.return_value.batchDeleteContacts.assert_called_once_with(
            body={"resourceNames": ["people/123"]}
        )

    def test_replace_mode_deletes_then_batch_imports_without_prompt(self) -> None:
        service = Mock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {
                    "resourceName": "people/123",
                    "metadata": {"sources": [{"type": "CONTACT"}]},
                }
            ]
        }
        service.people.return_value.batchDeleteContacts.return_value.execute.return_value = {}
        service.people.return_value.batchCreateContacts.return_value.execute.return_value = {
            "createdPeople": [{"person": {"resourceName": "people/new"}}]
        }

        with (
            patch.dict(
                os.environ,
                {
                    "ICLOUD_USERNAME": "person@example.com",
                    "ICLOUD_APP_PASSWORD": "not-a-real-password",
                    "GOOGLE_CLIENT_ID": "client-id",
                    "GOOGLE_CLIENT_SECRET": "client-secret",
                },
            ),
            patch("icloud_to_google.load_dotenv"),
            patch(
                "icloud_to_google.ICloudCardDAV"
            ) as card_dav_class,
            patch("icloud_to_google._google_credentials", return_value=object()),
            patch("icloud_to_google.build", return_value=service),
            patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
        ):
            card_dav_class.return_value.fetch_vcards.return_value = [
                ("contact.vcf", VCARD)
            ]
            result = sync(dry_run=False, replace_all=True)

        self.assertEqual(result, (1, 0, 0))
        service.people.return_value.batchDeleteContacts.assert_called_once()
        service.people.return_value.batchCreateContacts.assert_called_once()

    def test_batch_create_uses_one_request_for_contact_batch(self) -> None:
        contacts = [
            {"clientData": [{"key": "icloud_carddav_sync_id", "value": "marker-1"}]},
            {"clientData": [{"key": "icloud_carddav_sync_id", "value": "marker-2"}]},
        ]
        service = Mock()
        request = service.people.return_value.batchCreateContacts.return_value
        request.execute.return_value = {
            "createdPeople": [{"person": {}}, {"person": {}}]
        }

        created = _batch_create_google_contacts(service, contacts)

        self.assertEqual(created, 2)
        service.people.return_value.batchCreateContacts.assert_called_once()
        body = service.people.return_value.batchCreateContacts.call_args.kwargs["body"]
        self.assertEqual(body["contacts"], [{"contactPerson": item} for item in contacts])
        self.assertEqual(body["sources"], ["READ_SOURCE_TYPE_CONTACT"])

    def test_batch_create_splits_at_google_batch_limit(self) -> None:
        service = Mock()
        request = service.people.return_value.batchCreateContacts.return_value
        request.execute.side_effect = [
            {"createdPeople": [{"person": {}} for _ in range(200)]},
            {"createdPeople": [{"person": {}}]},
        ]

        created = _batch_create_google_contacts(
            service, [{"names": [{"givenName": str(index)}]} for index in range(201)]
        )

        self.assertEqual(created, 201)
        calls = service.people.return_value.batchCreateContacts.call_args_list
        self.assertEqual([len(call.kwargs["body"]["contacts"]) for call in calls], [200, 1])

    def test_batch_create_checks_for_contact_after_gateway_error(self) -> None:
        person = {
            "clientData": [
                {"key": "icloud_carddav_sync_id", "value": "icloud-uid-sha256:abc"}
            ]
        }
        service = Mock()
        batch_request = service.people.return_value.batchCreateContacts.return_value
        batch_request.execute.side_effect = HttpError(
            Mock(status=502), b"temporary failure"
        )
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [{"clientData": person["clientData"]}]
        }

        with patch("icloud_to_google.time.sleep") as sleep:
            created = _batch_create_google_contacts(service, [person])

        self.assertEqual(created, 1)
        service.people.return_value.batchCreateContacts.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_manual_oauth_accepts_localhost_redirect_url(self) -> None:
        flow = Mock()
        flow.authorization_url.return_value = (
            "https://accounts.google.test/auth",
            "state",
        )
        flow.credentials = Mock()
        flow.credentials.to_json.return_value = '{"token":"test"}'
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "token.json"
            with (
                patch(
                    "icloud_to_google.InstalledAppFlow.from_client_config",
                    return_value=flow,
                ),
                patch(
                    "builtins.input",
                    return_value="http://localhost/?code=code&state=state",
                ),
            ):
                credentials = _google_credentials(
                    Path("credentials.json"),
                    token_file,
                    "client-id",
                    "client-secret",
                    manual_auth=True,
                )

        self.assertIs(credentials, flow.credentials)
        self.assertEqual(flow.redirect_uri, "http://localhost")
        flow.fetch_token.assert_called_once_with(code="code")

    def test_manual_oauth_rejects_non_localhost_redirect(self) -> None:
        flow = Mock()
        flow.authorization_url.return_value = ("https://accounts.google.test/auth", "state")
        with (
            patch(
                "icloud_to_google.InstalledAppFlow.from_client_config",
                return_value=flow,
            ),
            patch(
                "builtins.input",
                return_value="http://localhost.attacker.test/?code=code&state=state",
            ),
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(ValueError, "localhost"),
        ):
            _google_credentials(
                Path("credentials.json"),
                Path(temp_dir) / "token.json",
                "client-id",
                "client-secret",
                manual_auth=True,
            )
        flow.fetch_token.assert_not_called()

    def test_manual_oauth_rejects_mismatched_state(self) -> None:
        flow = Mock()
        flow.authorization_url.return_value = ("https://accounts.google.test/auth", "expected")
        with (
            patch(
                "icloud_to_google.InstalledAppFlow.from_client_config",
                return_value=flow,
            ),
            patch(
                "builtins.input",
                return_value="http://localhost/?code=code&state=unexpected",
            ),
            tempfile.TemporaryDirectory() as temp_dir,
            self.assertRaisesRegex(ValueError, "Status"),
        ):
            _google_credentials(
                Path("credentials.json"),
                Path(temp_dir) / "token.json",
                "client-id",
                "client-secret",
                manual_auth=True,
            )
        flow.fetch_token.assert_not_called()

    def test_failure_email_uses_icloud_sender_without_exposing_password(self) -> None:
        smtp = Mock()
        with (
            patch.dict(
                os.environ,
                {
                    "ICLOUD_USERNAME": "person@icloud.com",
                    "ICLOUD_APP_PASSWORD": "never-email-this",
                    "FAILURE_EMAIL_TO": "lu_mueller@icloud.com",
                },
            ),
            patch("icloud_to_google.smtplib.SMTP") as smtp_factory,
        ):
            smtp_factory.return_value.__enter__.return_value = smtp
            _send_failure_email("test failure")

        smtp.login.assert_called_once_with("person@icloud.com", "never-email-this")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "lu_mueller@icloud.com")
        self.assertEqual(message["From"], "person@icloud.com")
        self.assertIn("person@icloud.com", message.get_content())
        self.assertIn("test failure", message.get_content())
        self.assertNotIn("never-email-this", message.get_content())

    def test_test_email_failure_does_not_trigger_another_email_attempt(self) -> None:
        with (
            patch("icloud_to_google.sys.argv", ["icloud_to_google.py", "--test-email"]),
            patch("icloud_to_google.load_dotenv"),
            patch(
                "icloud_to_google._send_failure_email",
                side_effect=RuntimeError("SMTP unavailable"),
            ),
            patch("icloud_to_google._notify_failure") as notify_failure,
        ):
            result = main()

        self.assertEqual(result, 1)
        notify_failure.assert_not_called()

    def test_default_cli_mode_is_full_replacement(self) -> None:
        with (
            patch("icloud_to_google.sys.argv", ["icloud_to_google.py"]),
            patch("icloud_to_google.load_dotenv"),
            patch("icloud_to_google.sync", return_value=(1, 0, 0)) as run_sync,
        ):
            result = main()

        self.assertEqual(result, 0)
        run_sync.assert_called_once_with(
            dry_run=False,
            replace_all=True,
            manual_oauth=False,
        )

    def test_incremental_flag_disables_full_replacement(self) -> None:
        with (
            patch(
                "icloud_to_google.sys.argv",
                ["icloud_to_google.py", "--incremental"],
            ),
            patch("icloud_to_google.load_dotenv"),
            patch("icloud_to_google.sync", return_value=(1, 0, 0)) as run_sync,
        ):
            result = main()

        self.assertEqual(result, 0)
        run_sync.assert_called_once_with(
            dry_run=False,
            replace_all=False,
            manual_oauth=False,
        )

    def test_create_does_not_blindly_retry_after_gateway_error(self) -> None:
        response = Mock(status=502)
        service = Mock()
        request = service.people.return_value.createContact.return_value
        request.execute.side_effect = HttpError(response, b"temporary failure")

        with self.assertRaises(HttpError):
            _create_google_contact(service, {"names": [{"givenName": "Jane"}]})

        request.execute.assert_called_once_with()

    def test_create_retries_after_wait_and_marker_check(self) -> None:
        class FakeRequest:
            def __init__(self, action):
                self.action = action

            def execute(self):
                return self.action()

        service = Mock()
        people_api = service.people.return_value
        people_api.createContact.side_effect = [
            FakeRequest(
                lambda: (_ for _ in ()).throw(
                    HttpError(Mock(status=502), b"temporary failure")
                )
            ),
            FakeRequest(lambda: {"resourceName": "people/123"}),
        ]
        people_api.connections.return_value.list.return_value = FakeRequest(
            lambda: {"connections": []}
        )

        with patch("icloud_to_google.time.sleep") as sleep:
            result = _create_with_recovery(
                service,
                {"names": [{"givenName": "Jane"}]},
                "icloud-uid-sha256:abc",
                set(),
                index=153,
                total=166,
            )

        self.assertEqual(result["resourceName"], "people/123")
        self.assertEqual(people_api.createContact.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_create_recovers_contact_saved_before_gateway_error(self) -> None:
        class FakeRequest:
            def execute(self):
                raise HttpError(Mock(status=502), b"temporary failure")

        service = Mock()
        people_api = service.people.return_value
        people_api.createContact.return_value = FakeRequest()
        saved_contact = {
            "resourceName": "people/already-saved",
            "userDefined": [
                {
                    "key": "icloud_carddav_sync_id",
                    "value": "icloud-uid-sha256:abc",
                }
            ],
        }
        people_api.connections.return_value.list.return_value.execute.return_value = {
            "connections": [saved_contact]
        }

        with patch("icloud_to_google.time.sleep") as sleep:
            result = _create_with_recovery(
                service,
                {"names": [{"givenName": "Jane"}]},
                "icloud-uid-sha256:abc",
                set(),
                index=153,
                total=166,
            )

        self.assertEqual(result, saved_contact)
        people_api.createContact.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_update_refreshes_etag_and_retries_after_gateway_error(self) -> None:
        existing = {
            "resourceName": "people/123",
            "etag": "etag-1",
            "metadata": {"sources": [{"type": "CONTACT", "id": "123"}]},
        }
        refreshed = {
            **existing,
            "etag": "etag-2",
            "metadata": {"sources": [{"type": "CONTACT", "id": "123"}]},
        }
        response = Mock(status=502)
        first_request = Mock()
        first_request.execute.side_effect = HttpError(
            response, b"temporary failure"
        )
        second_request = Mock()
        second_request.execute.return_value = {
            **refreshed,
            "names": [{"givenName": "Jane"}],
        }
        service = Mock()
        people_api = service.people.return_value
        people_api.updateContact.side_effect = [first_request, second_request]
        people_api.get.return_value.execute.return_value = refreshed

        with patch("icloud_to_google.time.sleep") as sleep:
            result = _update_google_contact(
                service,
                existing,
                {"names": [{"givenName": "Jane"}]},
            )

        self.assertEqual(result["names"], [{"givenName": "Jane"}])
        self.assertEqual(people_api.updateContact.call_count, 2)
        self.assertEqual(
            people_api.updateContact.call_args_list[1].kwargs["body"]["etag"],
            "etag-2",
        )
        sleep.assert_called_once_with(5)

    def test_finds_existing_by_email_phone_or_sync_marker(self) -> None:
        source = {
            "emailAddresses": [{"value": "jane@example.com"}],
            "phoneNumbers": [{"value": "+1 (555) 555-5555"}],
        }
        by_email = {"emailAddresses": [{"value": "JANE@example.com"}]}
        by_phone = {"phoneNumbers": [{"value": "15555555555"}]}
        by_marker = {
            "userDefined": [
                {"key": "icloud_carddav_sync_id", "value": "uid:contact-123"}
            ]
        }

        self.assertEqual(
            find_matches(source, "uid:contact-123", [by_email, by_phone, by_marker]),
            [by_email, by_phone, by_marker],
        )

    def test_update_replaces_icloud_fields_and_preserves_google_custom_fields(self) -> None:
        existing = {
            "resourceName": "people/123",
            "etag": "etag-123",
            "metadata": {
                "sources": [
                    {"type": "CONTACT", "id": "123", "etag": "source-etag"}
                ]
            },
            "userDefined": [
                {"key": "Personal note", "value": "keep"},
                {"key": "icloud_carddav_sync_id", "value": "uid:old"},
                {"key": "iCloud: CATEGORIES", "value": "old"},
                {"key": "iCloud: X-ABADR", "value": "de"},
            ],
            "clientData": [
                {"key": "other-app-key", "value": "keep"},
                {"key": "icloud_carddav_sync_id", "value": "old-hidden-marker"},
            ],
        }
        source = {
            "names": [{"givenName": "Jane"}],
            "userDefined": [
                {"key": "iCloud: CATEGORIES", "value": "new"},
            ],
            "clientData": [
                {"key": "icloud_carddav_sync_id", "value": "new-hidden-marker"}
            ],
        }

        body = _google_update_body(existing, source)

        self.assertEqual(body["names"], [{"givenName": "Jane"}])
        self.assertEqual(body["emailAddresses"], [])
        self.assertEqual(body["phoneNumbers"], [])
        self.assertEqual(body["externalIds"], [])
        self.assertEqual(body["locales"], [])
        self.assertEqual(len([field for field in MANAGED_FIELDS if field in body]), len(MANAGED_FIELDS))
        self.assertIn(
            {"key": "Personal note", "value": "keep"}, body["userDefined"]
        )
        self.assertNotIn(
            {"key": "iCloud: CATEGORIES", "value": "old"}, body["userDefined"]
        )
        self.assertIn(
            {"key": "iCloud: CATEGORIES", "value": "new"}, body["userDefined"]
        )
        self.assertNotIn(
            {"key": "iCloud: X-ABADR", "value": "de"}, body["userDefined"]
        )
        self.assertNotIn(
            {"key": "icloud_carddav_sync_id", "value": "uid:new"},
            body["userDefined"],
        )
        self.assertIn(
            {"key": "other-app-key", "value": "keep"}, body["clientData"]
        )
        self.assertIn(
            {"key": "icloud_carddav_sync_id", "value": "new-hidden-marker"},
            body["clientData"],
        )
        self.assertNotIn(
            {"key": "icloud_carddav_sync_id", "value": "old-hidden-marker"},
            body["clientData"],
        )

    def test_removes_previously_synced_google_photo(self) -> None:
        service = Mock()
        people_api = service.people.return_value
        people_api.deleteContactPhoto.return_value.execute.return_value = {
            "person": {"resourceName": "people/123", "photos": []}
        }

        _remove_synced_google_photo(
            service,
            {
                "resourceName": "people/123",
                "photos": [{"url": "photo"}],
                "userDefined": [
                    {"key": "icloud_carddav_photo_sha256", "value": "old-hash"}
                ],
            },
        )

        people_api.deleteContactPhoto.assert_called_once_with(
            resourceName="people/123",
            personFields="metadata,photos,userDefined",
        )

    def test_photo_delete_confirms_state_after_gateway_error(self) -> None:
        contact = {
            "resourceName": "people/123",
            "photos": [{"url": "photo"}],
            "userDefined": [
                {"key": "icloud_carddav_photo_sha256", "value": "old-hash"}
            ],
        }
        service = Mock()
        people_api = service.people.return_value
        people_api.deleteContactPhoto.return_value.execute.side_effect = HttpError(
            Mock(status=502), b"temporary failure"
        )
        people_api.get.return_value.execute.return_value = {
            **contact,
            "photos": [],
        }

        with patch("icloud_to_google.time.sleep") as sleep:
            result = _remove_synced_google_photo(service, contact)

        self.assertEqual(result["photos"], [])
        people_api.deleteContactPhoto.assert_called_once()
        sleep.assert_called_once_with(5)

    def test_repeated_sync_updates_existing_contact_and_clears_removed_fields(self) -> None:
        class FakeRequest:
            def __init__(self, action):
                self.action = action

            def execute(self):
                return self.action()

        class FakePeople:
            def __init__(self):
                self.contacts = []
                self.update_calls = 0

            def connections(self):
                return self

            def list(self, **_kwargs):
                return FakeRequest(lambda: {"connections": self.contacts.copy()})

            def createContact(self, body):
                contact = {
                    **body,
                    "resourceName": "people/123",
                    "etag": "etag-1",
                    "metadata": {
                        "sources": [
                            {"type": "CONTACT", "id": "123", "etag": "source-1"}
                        ]
                    },
                }
                self.contacts.append(contact)
                return FakeRequest(lambda: contact)

            def updateContact(self, *, resourceName, body, **_kwargs):
                self.update_calls += 1
                contact = next(
                    item
                    for item in self.contacts
                    if item["resourceName"] == resourceName
                )
                contact.update(body)
                contact["etag"] = f"etag-{self.update_calls + 1}"
                contact["metadata"] = {
                    "sources": [
                        {
                            "type": "CONTACT",
                            "id": "123",
                            "etag": f"source-{self.update_calls + 1}",
                        }
                    ]
                }
                return FakeRequest(lambda: contact.copy())

        class FakeService:
            def __init__(self):
                self.people_api = FakePeople()

            def people(self):
                return self.people_api

        service = FakeService()
        changed_vcard = VCARD.replace("Jane", "Janet").replace(
            "TEL;TYPE=CELL:+1-555-555-5555\n", ""
        )
        card_reader = Mock()
        card_reader.fetch_vcards.side_effect = [
            [("contact.vcf", VCARD)],
            [("contact.vcf", changed_vcard)],
        ]

        with (
            patch.dict(
                os.environ,
                {
                    "ICLOUD_USERNAME": "person@example.com",
                    "ICLOUD_APP_PASSWORD": "not-a-real-password",
                    "GOOGLE_CLIENT_ID": "client-id",
                    "GOOGLE_CLIENT_SECRET": "client-secret",
                },
            ),
            patch("icloud_to_google.load_dotenv"),
            patch("icloud_to_google.ICloudCardDAV", return_value=card_reader),
            patch("icloud_to_google._google_credentials", return_value=object()),
            patch("icloud_to_google.build", return_value=service),
        ):
            first_result = sync(dry_run=False)
            second_result = sync(dry_run=False)

        self.assertEqual(first_result, (1, 0, 0))
        self.assertEqual(second_result, (0, 1, 0))
        self.assertEqual(service.people_api.update_calls, 1)
        self.assertEqual(service.people_api.contacts[0]["names"][0]["givenName"], "Janet")
        self.assertEqual(service.people_api.contacts[0]["phoneNumbers"], [])


if __name__ == "__main__":
    unittest.main()
