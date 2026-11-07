# Riffle

Search every file on your Windows PC the moment you think of it, see what's inside without opening it, and get your disk space back.

- **Ctrl + Shift + Space** from anywhere opens Riffle. Results appear as you type, across half a million files in a few milliseconds.
- **Quick bar**, like Spotlight: files, apps, Windows settings, browser history and bookmarks, sums like `18% of 4,500`, web addresses and a web-search row, all in one box, grouped under a Top hit. It learns what you open, shows words inside your documents (from Windows Search), previews the selected result, and comes in Paper or Glass.
- **Preview** with Space: images, video, audio, PDFs, text and code, plus Explorer thumbnails for Office files and anything else Windows can preview.
- **Duplicates**: finds files with identical contents, even when renamed. One copy of each is always kept.
- **Clean up**: temp files, browser caches, old installers, forgotten downloads, rebuildable project folders, big old files. Every item says why it's safe. Your files go to the Recycle Bin.
- **Space map**: see which folders take the most room and drill in.
- **Visual search** (optional download): drop in an image or video and find its original, even after crops, filters or resizing.

Everything runs on your PC. Riffle never uploads your files and works offline.

## Install

Download `Riffle-Setup-x.y.z.exe` from [Releases](https://github.com/ravirahul/riffle/releases/latest) and run it. No admin rights needed.

## Light on your PC

- One background process. It sleeps until Windows reports a file change, and batches those changes every few seconds while you're not searching.
- Duplicate and cleanup scans only run when you ask, at low priority.
- Dependency and system stores (`node_modules`, `WinSxS`, `site-packages`) are listed but not indexed inside, which keeps memory low.
- The index is saved as it changes, and caught up quietly after a restart.

## Build from source

Needs Python 3.14 with `numpy` and `comtypes` (plus `pillow opencv-python-headless openvino` for visual search), PyInstaller and Inno Setup 6.

```
python src\riffle.py            # run from source
packaging\build.bat             # installer + visual pack in dist\
```

Release steps are in [docs/PUBLISHING.md](docs/PUBLISHING.md).
