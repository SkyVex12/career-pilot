# CoRuby identity

CoRuby uses the coral gem in `apps/desktop/src/assets/xruby-icon.svg`. Its six polygon facets
are the approved source geometry; the separate wordmark has a neutral Co prefix and coral
Ruby suffix (`#BE503A` in light mode, `#FF9B82` in dark mode). The desktop
already used this gem and the approved coral tokens. Mobile uses the same facets, including
its native launcher and splash assets.

The identity color is `#F05A3C`. Accessible actions use `#BE503A` in light mode and `#E9826C`
in dark mode; dark accent text uses `#FF9B82`. Status colors and syntax highlighting retain
their semantic meanings. HubRuby is the account/application integration name.

## Installation and protocol compatibility

Display strings, macOS bundle display metadata, tray menus, window titles, new default
Task Manager names, sign-in pages, and mobile launcher labels say CoRuby. Existing user
custom display names are retained.

Tauri's `productName` remains `Copilot Desktop`. Tauri derives installation directories,
Start Menu names, registry keys, and the default WiX upgrade code from it. Changing that
field in a cosmetic release could create a second installation or break upgrades. Therefore
the Windows installer/shortcut and signed executable metadata may retain the legacy name;
the macOS `.app` filename and packaging filenames also remain stable. A future installer
migration must handle those identities explicitly before changing `productName`.

The bundle ID, mobile package IDs and URL schemes, native project names, OAuth client IDs,
JWT audiences, API endpoints, database/storage keys, `RubyCopilot` login registration, and
`Copilot-*` release download aliases are unchanged. Internal `copilot` package names remain
unchanged. The new UI labels do not change the integration protocol.

## Validation and publication

The design harness uses the real desktop UI with fictional data:
`docs/design/harness/vite.config.mjs`. Inspect setup with `?data=signedout`, the workspace with
`?call=midcall`, and the overlay with `?call=midcall#overlay`, in both themes.

Opening a pull request runs checks. Deployment is triggered only by a push to `main` or
manual dispatch; release publication is triggered by the deployment workflow or a
`desktop-v*` tag. This identity change does not alter those triggers.
