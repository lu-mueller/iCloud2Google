# iCloud-Kontakte mit Google synchronisieren

Das Skript synchronisiert iCloud-Kontakte über CardDAV nach Google Kontakte.
Standardmäßig ersetzt jeder Lauf alle normalen Google-Kontakte durch den
aktuellen iCloud-Bestand. Google-Profil und automatisch gespeicherte Kontakte
bleiben erhalten. Alternativ legt `--incremental` neue Kontakte an und
überschreibt bei eindeutigen Treffern die von iCloud verwalteten Felder;
Felder, die in iCloud nicht mehr vorhanden sind, werden dabei geleert.
Im inkrementellen Modus bleiben Google-eigene benutzerdefinierte Felder
erhalten.

Die Synchronisierung ist einseitig von iCloud nach Google. Im Standardmodus
werden auch Google-Kontakte gelöscht, die nicht in iCloud vorhanden sind.
`--incremental` löscht keine Google-Kontakte und überträgt iCloud-Löschungen
nicht. E-Mail-Adressen, Telefonnummern und die gespeicherte iCloud-Kennung
helfen, Kontakte im inkrementellen Modus wiederzufinden.

## Voraussetzungen

- Python 3.10 oder neuer
- Ein iCloud-App-spezifisches Passwort
- OAuth-Client-Zugangsdaten für die Google People API

## Einrichtung

1. Erstelle ein App-spezifisches Passwort unter
   [account.apple.com](https://account.apple.com/) und aktiviere die
   [Google People API](https://console.cloud.google.com/apis/library/people.googleapis.com)
   in einem Google-Cloud-Projekt.
2. Erstelle in Google Cloud unter **APIs & Dienste → Anmeldedaten** eine
   OAuth-Client-ID vom Typ **Desktop-App**. Lade die JSON-Datei herunter und
   speichere sie im Projektordner als `credentials.json`.
3. Lege eine lokale Konfigurationsdatei an und trage dort die Zugangsdaten ein:

   ```sh
   cp .env.example .env
   ```

   In `.env` kommen `ICLOUD_USERNAME` und `ICLOUD_APP_PASSWORD`. Für Google
   kannst du entweder `GOOGLE_CLIENT_ID` und `GOOGLE_CLIENT_SECRET` eintragen
   oder die OAuth-JSON-Datei als `GOOGLE_OAUTH_CLIENT_FILE` referenzieren.
   `GOOGLE_TOKEN_FILE` bestimmt den lokalen Speicherort des OAuth-Tokens.
   `.env` wird nicht in Git gespeichert.

4. Installiere die Abhängigkeiten:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

5. Prüfe zuerst, was der vollständige Neuimport ändern würde:

   ```sh
   python icloud_to_google.py --dry-run
   ```

6. Starte den vollständigen Neuimport:

   ```sh
   python icloud_to_google.py
   ```

**Achtung:** Ohne weitere Option löscht das Skript alle normalen Google-
Kontakte und importiert iCloud neu. Es gibt keine zusätzliche Bestätigung.
Google-Profil und automatisch gespeicherte Kontakte bleiben bestehen.
Kontakte, die nur bei Google vorhanden sind, werden gelöscht. Mit
`--incremental` kannst du stattdessen die selektive Synchronisierung verwenden.

Für eine Anmeldung in einem Browser auf einem anderen Gerät:

   ```sh
   python icloud_to_google.py --oauth-manual --dry-run
   ```

Das Skript zeigt eine Google-Anmelde-URL an. Nach der Anmeldung kopierst du
die vollständige `http://localhost/...`-Weiterleitungs-URL aus der
Browser-Adresszeile zurück ins Terminal. Ohne `--dry-run` wird danach der
vollständige Neuimport ausgeführt.

Zum Testen der Fehler-Mail:

   ```sh
   python icloud_to_google.py --test-email
   ```

Fehler werden über `smtp.mail.me.com` mit dem iCloud-Konto und
App-spezifischen Passwort aus `.env` an `FAILURE_EMAIL_TO` gesendet. Die
Nachricht enthält Kontonamen und Fehlermeldung, aber keine Passwörter oder
Kontaktdaten.

Beim ersten Start öffnet sich die Google-Anmeldung im Browser. Das Skript
speichert das OAuth-Token lokal in `token.json`; diese Datei und
`credentials.json` sind in `.gitignore` eingetragen. Zugangsdaten können über
`GOOGLE_OAUTH_CLIENT_FILE` und `GOOGLE_TOKEN_FILE` an andere Dateipfade
verlegt werden.
Auf headless Linux-Systemen ohne installierten Browser wechselt das Skript
automatisch zur manuellen Anmeldung: Öffne die angezeigte URL auf einem
anderen Gerät und füge anschließend die vollständige `http://localhost/...`-
Rückleitungs-URL im Terminal ein.

Das Skript meldet jeden Synchronisierungsschritt mit Zeitstempel und
Kontaktzähler im Terminal. Vorübergehende Fehler beim Abrufen von
Google-Kontakten werden mit Wartezeit wiederholt. Bei einem vorübergehenden
Fehler beim Anlegen wartet das Skript 5, 15 und 30 Sekunden, prüft anhand
einer gehashten iCloud-Kennung, ob Google den Kontakt trotzdem angelegt hat,
und versucht es andernfalls begrenzt erneut. Der vollständige Neuimport legt
Kontakte in Batches zu höchstens 200 Einträgen an. Bei Fehlern beim
Aktualisieren oder Entfernen eines Fotos wird der aktuelle Kontakt zuerst neu
geladen; ein veralteter ETag wird so vor einem Wiederholungsversuch vermieden.

Das Skript überträgt unter anderem Namen, Spitznamen, E-Mail-Adressen,
Telefonnummern, Organisationen, Titel/Rollen, Adressen einschließlich
Postfach, Zusatz, Straße, Ort, Region, Postleitzahl und Land, Geburtstage,
Jahrestage, URLs, Kalender-URLs, Notizen, Beziehungen und Geschlecht, soweit
sie in den iCloud-vCards vorhanden sind.
Weitere einfache vCard-Felder werden nach Möglichkeit als benutzerdefinierte
Google-Kontaktfelder abgelegt; `X-ABADR`, `X-ABSHOWAS`, IBAN-Felder,
Sprachangaben, iCloud-UIDs als externe IDs und Kontaktfotos werden nicht
übernommen. Die gehashte Kennung zur Duplikaterkennung wird in Googles nicht
angezeigten `clientData` gespeichert, nicht als sichtbares benutzerdefiniertes
Kontaktfeld. Bei bereits synchronisierten Kontakten werden alte sichtbare
iCloud-Kennungen, `X-ABADR`-/`X-ABSHOWAS`-/IBAN-Felder und früher importierte
Fotos entfernt.
