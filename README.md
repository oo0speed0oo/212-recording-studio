# 212 Recording Studio

A small tool for recording your own voice for the 212 English Phonics pages.

You read from a scrolling teleprompter into one long recording. The program cuts it up,
names every file the way the website needs it, and puts the files in the right folders.
No file work for you.

**Status: skeleton only (step R0). Nothing to run yet.**

## How to start it (available from step R1)

```
~/kokoro-env/bin/python ~/Documents/212-recording-studio/studio.py
```

Then open http://localhost:8212 (it opens by itself). Mac only for now.

## Settings

Copy `settings.example.json` to `settings.json` and change the website folder if yours is
somewhere else. The studio only **reads** the CSV lists from that folder.

## Folders (created when you use the studio)

| Folder / file | What it holds | Uploaded to GitHub? |
|---|---|---|
| `takes/` | the raw long recordings, saved every 30 seconds | no |
| `output/` | the finished `.m4a` files, in the website's folder layout | no |
| `output-wav/` | a lossless copy of every cut, so you can reprocess later | no |
| `progress.json` | which items are done | no |
| `settings.json` | your settings | no |

Nothing in this repo ever touches the live website.
