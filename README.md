# 212 Recording Studio

A small tool for recording your own voice for the 212 English Phonics pages.

You read from a scrolling teleprompter into one long recording. The program finds each item,
and (from step R2) cuts the recording up, names every file the way the website needs it,
and puts the files in the right folders. No file work for you.

**Status: step R1.** The Sounds teleprompter records and saves your raw takes and the start/end time of every
sound. Cutting, naming and the `.m4a` files come in step R2.

## Start it

Type this in Terminal (nothing to install):

```
~/kokoro-env/bin/python ~/Documents/212-recording-studio/studio.py
```

Your browser opens http://localhost:8212 by itself. Keep the Terminal window open while you record.
To stop: press Ctrl+C in that window. Mac only for now. Chrome or Safari both work; the first time, allow the microphone.

## How a session goes

1. **Pick a list** (only *Sounds* works so far).
2. **Quiet check** (2 seconds): stay quiet. The program measures the room and sets the detection level.
   Pick your microphone here (the Blue one) and check the volume bar moves when you speak.
3. **Teleprompter:** the current sound is big in the blue band. Say it **once**. When you stop speaking
   (half a second of quiet) it gets a red check and the list moves on. You touch nothing.

| Key | Button | What it does |
|---|---|---|
| ← | Redo | go back one item and say it again |
| → | Skip | leave this item undone and move on |
| Space | Pause | stop listening and stop the recording clock |
| R | Stop & retry | the sound was missed: listen again, more sensitive (3 steps, each also shortens the "must last" time) |
| hold Enter | Hold to record | fallback: while you hold it, the take is forced. No detection needed |
| click a row | | go to that item and redo it. By default you return to where you were after one take |

Under the list: choose what a click on a finished item does: **Only this one**, **From here** (carries on forward and
redoes everything after it) or a **Range** (for example items 20 to 30).
**Noise suppression** (top right) can be switched on or off any time; the program re-measures the room for 2 seconds.
**Finish** saves everything and returns to the lists.

The white line on the volume bar is the detection level: your voice must rise above it.

## What gets saved (in `takes/`)

* `takes/<date>_<time>_sounds.wav`: the raw recording (16-bit, mono). Saved every 30 seconds while you record,
  so a crash loses at most 30 seconds.
* `takes/<date>_<time>_sounds.json`: the start and end time of every item, plus loudness, which mic and
  noise-suppression setting, and a log of what you pressed.

Next time the list starts at the first item that is not recorded yet. **Start Sounds over** (bottom of the list
screen) moves your unfinished takes to `takes/_old/`. Nothing is ever deleted.

## Settings

Copy `settings.example.json` to `settings.json` if you need to change something:

* `website_folder`: where the website is (default `~/Documents/212english-website`). The studio only **reads** the CSV lists there.
* `mic_tip`: the reminder shown on the quiet-check screen.

## Folders

| Folder / file | What it holds | Uploaded to GitHub? |
|---|---|---|
| `takes/` | the raw long recordings | no |
| `output/` | the finished `.m4a` files (from step R2) | no |
| `output-wav/` | a lossless copy of every cut (from step R2) | no |
| `progress.json` | which items are done (from step R2) | no |
| `settings.json` | your settings | no |

Nothing in this repo ever touches the live website.

## Tests (no microphone needed)

```
tests/run_tests.sh
```

Makes synthetic audio (tones and hiss with silences), runs the voice detector, the Redo/Skip logic and the server
on it, and says ALL TESTS PASSED. To watch the whole screen run on test audio instead of a microphone, start the studio
and open `http://localhost:8212/?testaudio=flow.wav&list=sounds&autostart=1` after running the tests once.
