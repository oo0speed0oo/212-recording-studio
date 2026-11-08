# 212 Recording Studio

A small tool for recording your own voice for the 212 English Phonics pages.

You read from a scrolling teleprompter into one long recording. The program finds each item,
and (from step R2) cuts the recording up, names every file the way the website needs it,
and puts the files in the right folders. No file work for you.

**Status: step R2.** The Sounds teleprompter records you, and **Finish** cuts every sound out, evens out the loudness,
names the files the way the website needs them and saves them. The check screen (play each file, OK / Redo) comes in step R3.

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
**Finish** saves your recording, then cuts and files everything (a few seconds) and shows what it made.

The white line on the volume bar is the detection level: your voice must rise above it.

## What Finish makes

For every sound you recorded:

| File (in `output/phonics/`) | What it is |
|---|---|
| `alone/<no>.m4a` | the single sound (version 1) |
| `alone/<no>-2.m4a` | the single second version, for the 8 two-version sounds (th, ew, eu, ei, ue, sion, ea, ch). The site already uses this name |
| `sounds/<no>.m4a` | the sound **3 times** with 0.6 s between. Two-version sounds: version 1 three times, a 1 s pause, version 2 three times |

You only say each sound once; the program makes the repeats and the gaps. A two-version sound gets its `sounds/` file
once **both** versions are recorded (they can be on different days).

How each sound is prepared: cut out of the long recording with 0.12 s of quiet either side, hold-to-record silence trimmed,
10 ms fade in and out, then set to the same loudness (RMS about −20 dB; the loudest peak never goes above −1 dB, which wins when they
disagree). Files from different days sound equally loud. Saved as AAC `.m4a`, 64 kbps.

The result screen lists every file with its length and loudness, and warns about anything suspicious: clipped (too loud),
very quiet (mic too far, or the sound was missed), very short (a quick /k/ may be cut).
If something went wrong or you closed the window without pressing Finish, the lists screen says "N recorded items are waiting
to be cut" with a button. Your recordings are never lost.

**Redo a sound later:** record it again (click it in the list). Finish replaces just that sound's files. The older cut is moved to
`output-wav/_replaced/`, never deleted.

## What gets saved

* `takes/<date>_<time>_sounds.wav`: the raw recording (16-bit, mono), saved every 30 seconds while you record,
  so a crash loses at most 30 seconds. Kept until you delete it.
* `takes/<date>_<time>_sounds.json`: the start and end time of every item, loudness, which mic and noise-suppression setting,
  and a log of what you pressed.
* `output-wav/cuts/sounds/`: a **lossless copy of every cut** (raw, with 0.4 s of room noise either side) and `index.json`.
  Everything in `output/` is rebuilt from these.
* `progress.json`: which items are done. The list starts at the first item not done.

**Rebuild all Sounds files** (bottom of the lists screen) remakes every file from the saved cuts with the current settings.
Use it after changing the loudness, bitrate or gaps below. No re-recording.

**Start Sounds over** moves recordings that are not cut yet to `takes/_old/`. **Start Sounds over completely** also moves the finished
files, the cuts and the progress there. Nothing is ever deleted.

## Settings

Copy `settings.example.json` to `settings.json` to change anything (then press **Rebuild** to apply it to files you already made):

| Setting | Default | What it does |
|---|---|---|
| `website_folder` | `~/Documents/212english-website` | where the CSV lists are. The studio only **reads** there |
| `mic_tip` | (a reminder) | the text on the quiet-check screen |
| `pad_s` | 0.12 | quiet kept before and after each sound |
| `fade_ms` | 10 | fade in and out |
| `target_rms_db` | −20 | loudness of the sound (higher = louder, up to −6) |
| `peak_db` | −1 | the loudest sample never goes above this |
| `aac_kbps` | 64 | quality. Try 96 or 128 if short sounds like /k/ and /t/ sound rough |
| `sound_repeats` | 3 | times the sound is said in `sounds/<no>.m4a` |
| `sound_gap_s` | 0.6 | quiet you hear between repeats |
| `version_pause_s` | 1.0 | quiet you hear between version 1 and version 2 |

## Folders

| Folder / file | What it holds | Uploaded to GitHub? |
|---|---|---|
| `takes/` | the raw long recordings | no |
| `output/` | the finished `.m4a` files, in the website's folder layout | no |
| `output-wav/` | a lossless copy of every cut | no |
| `progress.json` | which items are done | no |
| `settings.json` | your settings | no |

Nothing in this repo ever touches the live website.

## Tests (no microphone needed)

```
tests/run_tests.sh
```

Makes synthetic audio (tones and hiss with silences), runs the voice detector, the Redo/Skip logic, the server and the whole
cut-clean-encode step on it (decoding the `.m4a` files back to check lengths, gaps, loudness and bitrate), and says ALL TESTS PASSED. To watch the whole screen run on test audio instead of a microphone, start the studio
and open `http://localhost:8212/?testaudio=flow.wav&list=sounds&autostart=1` after running the tests once.
