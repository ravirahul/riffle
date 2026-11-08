"""Build the Microsoft Store package: build/dist/Riffle -> dist/Riffle-<version>.msix

The Store signs the package itself, so no certificate is needed. Fill packaging/store.json
with the three values from Partner Center › Riffle › Product identity first.
Needs the Windows SDK (makeappx.exe): winget install Microsoft.WindowsSDK.10.0.26100
"""
import glob
import json
import os
import shutil
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, 'build', 'dist', 'Riffle')
OUT = os.path.join(ROOT, 'build', 'msix')

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:desktop="http://schemas.microsoft.com/appx/manifest/desktop/windows10"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
         IgnorableNamespaces="uap desktop rescap">
  <Identity Name="{name}" Publisher="{publisher}" Version="{version}.0" ProcessorArchitecture="x64"/>
  <Properties>
    <DisplayName>Riffle</DisplayName>
    <PublisherDisplayName>{publisher_name}</PublisherDisplayName>
    <Logo>assets\\StoreLogo.png</Logo>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.26100.0"/>
  </Dependencies>
  <Resources><Resource Language="en-us"/></Resources>
  <Applications>
    <Application Id="Riffle" Executable="Riffle.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="Riffle" Description="Search and clean up every file on your PC"
          BackgroundColor="transparent" Square150x150Logo="assets\\Square150x150Logo.png"
          Square44x44Logo="assets\\Square44x44Logo.png">
        <uap:DefaultTile Wide310x150Logo="assets\\Wide310x150Logo.png"/>
      </uap:VisualElements>
      <Extensions>
        <!-- RiffleStart.exe is a copy of Riffle.exe that starts without a window (see riffle.py) -->
        <desktop:Extension Category="windows.startupTask" Executable="RiffleStart.exe" EntryPoint="Windows.FullTrustApplication">
          <desktop:StartupTask TaskId="RiffleStartup" Enabled="true" DisplayName="Riffle"/>
        </desktop:Extension>
      </Extensions>
    </Application>
  </Applications>
  <Capabilities>
    <rescap:Capability Name="runFullTrust"/>
    <DeviceCapability Name="microphone"/>
  </Capabilities>
</Package>
"""


def makeappx():
    hits = sorted(glob.glob(r'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\makeappx.exe'))
    if not hits:
        sys.exit('makeappx.exe not found: winget install Microsoft.WindowsSDK.10.0.26100')
    return hits[-1]


def logos(dst):
    os.makedirs(dst, exist_ok=True)
    ico = Image.open(os.path.join(HERE, 'riffle.ico'))
    ico.size = max(ico.ico.sizes())                     # the biggest frame in the .ico
    src = ico.convert('RGBA')
    for name, w, h in (('StoreLogo', 50, 50), ('Square44x44Logo', 44, 44), ('Square150x150Logo', 150, 150),
                       ('Wide310x150Logo', 310, 150), ('Square44x44Logo.targetsize-256_altform-unplated', 256, 256)):
        side = min(w, h) if w == h else int(h * 0.7)
        canvas = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        icon = src.resize((side, side), Image.LANCZOS)
        canvas.paste(icon, ((w - side) // 2, (h - side) // 2), icon)
        canvas.save(os.path.join(dst, name + '.png'))


def main():
    cfg = json.load(open(os.path.join(HERE, 'store.json'), encoding='utf-8-sig'))
    if 'FILL' in cfg['name'] + cfg['publisher']:
        sys.exit('Fill packaging/store.json from Partner Center › Product identity first.')
    version = open(os.path.join(HERE, 'version.txt'), encoding='utf-8').read().split("filevers=(")[1].split(')')[0]
    version = '.'.join(version.replace(' ', '').split(',')[:3])
    if not os.path.exists(os.path.join(APP, 'Riffle.exe')):
        sys.exit('Build the app first (PyInstaller, see build.bat).')
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(APP, OUT)
    shutil.copy2(os.path.join(OUT, 'Riffle.exe'), os.path.join(OUT, 'RiffleStart.exe'))
    logos(os.path.join(OUT, 'assets'))
    with open(os.path.join(OUT, 'AppxManifest.xml'), 'w', encoding='utf-8') as f:
        f.write(MANIFEST.format(version=version, **cfg))
    out = os.path.join(ROOT, 'dist', f'Riffle-{version}.msix')
    subprocess.run([makeappx(), 'pack', '/o', '/d', OUT, '/p', out], check=True)
    print('built', out)


if __name__ == '__main__':
    main()
