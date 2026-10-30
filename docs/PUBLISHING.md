# Publishing Riffle

What it takes to put Riffle in front of people, in order.

## 1. GitHub repo and releases (needed for downloads, updates and the visual pack)

1. Create the repo `ravirahul/riffle` on GitHub. It must be **public**, because the app checks
   `api.github.com/repos/ravirahul/riffle/releases/latest` without signing in. If you want the
   source private, make a separate public repo just for releases and change `GITHUB_REPO` in
   `src/riffle.py` to match.
2. Push: `git remote add origin https://github.com/ravirahul/riffle.git` then `git push -u origin main`.
3. Run `packaging\build.bat`. It creates in `dist\`:
   - `Riffle-Setup-1.1.0.exe`: the installer people download
   - `riffle-visual-pack.zip` and `riffle-visual-pack.zip.sha256`: the optional visual-search download
4. On GitHub: Releases, then Draft a new release, tag `v1.1.0`, and attach all three files.
   The app finds the pack and the update by these exact file names.

Each new version: bump `VERSION` in `src/riffle.py`, `AppVer` in `packaging/riffle.iss` and
`packaging/version.txt`, build, and publish a release with a higher tag. Installed copies show
"Riffle x.y.z is ready · Update" in the footer within a day.

## 2. Getting past "Windows protected your PC"

New, unsigned apps trigger a SmartScreen warning. Most people stop there. Options, cheapest first:

| Route | Cost | Effect |
|---|---|---|
| **Microsoft Store** | Free for individual developers | The Store signs the package. No warning, automatic updates, Store search listing. |
| Certum open-source code signing certificate | about €30 to €60 a year | Signed exe. The warning fades as downloads build reputation. Needs public source. |
| Standard OV code signing certificate | about $200 to $400 a year | Same as above, for closed source. |
| Azure Trusted Signing | about $10 a month | Cheapest signing, but individual developers must currently be in the US or Canada. |

Recommended: Microsoft Store first, and also offer the GitHub download.

### Microsoft Store (Win32 installer route)

1. Register at https://storedeveloper.microsoft.com as an individual developer (free).
2. Partner Center, then Apps and games, then New product, then **EXE or MSI app**. Reserve the name "Riffle".
3. Package URL: the direct GitHub download link of `Riffle-Setup-x.y.z.exe`
   (for example `https://github.com/ravirahul/riffle/releases/download/v1.1.0/Riffle-Setup-1.1.0.exe`).
   Installer parameters for silent install: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`.
4. Store listing: description (see README), at least one screenshot (take them from the app at
   1366×768 or larger), a privacy policy URL (see `site/privacy.html`), category Utilities & tools.
5. Submit. Certification usually takes 1 to 3 days.

## 3. winget (`winget install Riffle`)

After the first public release:

1. Install the helper: `winget install wingetcreate`
2. `wingetcreate new https://github.com/ravirahul/riffle/releases/download/v1.1.0/Riffle-Setup-1.1.0.exe`
   Use the package id `Bliink.Riffle`, the publisher `Bliink`, and the installer type `inno`.
3. It opens a pull request to `microsoft/winget-pkgs`. Once merged, anyone can run `winget install Bliink.Riffle`.
4. For later versions: `wingetcreate update Bliink.Riffle -u <new url> -v <version> --submit`.

## 4. The website

`site/` is a one-page download site (index + privacy policy). It can go on riffle.bliink.in
(Hostinger) or GitHub Pages. Its Download button points to the latest GitHub release, so it never
needs editing for new versions.
