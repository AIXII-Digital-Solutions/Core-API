# Certificate assets

`default_logo.svg` / `default_logo.png` are the AI12 logo the migration `certificates_settings` seeds
into `certificate.asset` (key `logo`). The images the certificates actually draw live in the
database and are managed from the portal's admin (`/certificates/settings/logo`, `/stamp`); these
files are only the initial seed.
