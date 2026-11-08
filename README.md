# 212 Recording Studio

A small tool for recording your own voice for the 212 English Phonics pages.

You read from a scrolling teleprompter into one long recording. The program finds each item,
and (from step R2) cuts the recording up, names every file the way the website needs it,
and puts the files in the right folders. No file work for you.

**Status: step R4.** Four lists (Sounds, Challenge words, Sentences, Common words). The teleprompter records you, **Finish** cuts
everything out, evens out the loudness, names the files the way the website needs them and saves them, and the **check screen**
lets you play every new file and press OK or Redo. The **⚙ Options** menu changes how it all behaves.

## Start it

Type this in Terminal (nothing to install):

```
~/kokoro-env/bin/python ~/Documents/212-recording-studio/studio.py
```

Your browser opens http://localhost:8212 by itself. Keep the Terminal window open while you record.
To stop: press Ctrl+C in that window. Mac only for now. Chrome or Safari both work; the first time, allow the microphone.

## How a session goes

1. **Pick a list.** *Sounds* (75 items: 67 sounds, 8 with two versions), *Challenge words* (rare words and names, in lesson order),
   *Sentences* (in id order) and *Common words* (do these last). Each card shows how many are done.
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
| Enter (sentences) | Done | **Sentences only:** ends the take right now, so you do not wait for the silence. Hold-to-record is the **H** key there |
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

After Finish the **check screen** opens. It warns about anything suspicious: clipped (too loud),
very quiet (mic too far, or the sound was missed), very short (a quick /k/ may be cut).
If something went wrong or you closed the window without pressing Finish, the lists screen says "N recorded items are waiting
to be cut" with a button. Your recordings are never lost.

## The check screen

Every new file is listed with its length and loudness. For each file:

* **▶** plays it (press again to stop). **Play all** plays every file on the screen in order. **■ Stop** stops.
* **Compare with robot** (shown when the website has a robot file of the same name) plays the old robot voice, then yours.
  The studio only reads the website's audio folder; it never changes it.
* **OK** means you are happy with it. **Redo** puts that sound back in the Sounds list (amber ↻) so you record it again next time.
  Press the same button again to take it back. **OK all not yet checked** marks everything on the screen OK.
* Redo on `alone/…` sends just that sound back. Redo on `sounds/…` sends the sound (both versions for a two-version sound) back.
* Files you have already checked are hidden; tick **Show files I already checked** to see them all.
* Closed it by mistake? The lists screen says "N files are waiting for you to check" with a button.

Re-recording a sound and pressing Finish replaces its files, and they come back unchecked.

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

## Options (the ⚙ button)

Everything below can be changed in the **⚙ Options** menu (lists screen, quiet-check screen, or while recording) and is saved in
`settings.json`. Nothing to type by hand. **Put all options back to the defaults** is at the bottom.

**Recording**
* **Noise suppression** (on). It can clip faint consonants. **Test the mic** (lists screen): you say the same thing twice for 3 seconds,
  once with noise suppression on and once off; play both and press "Use" under the better one.
* **Mode:** *Quick* (default): record many in a row and check later. *Careful*: each take is played back right after it is found
  (the recording pauses while it plays, so the playback is never recorded). Press ← if you did not like it.
* **After redoing an old item:** return to where you were (default), or stay on the redone item.
* **Countdown beat** (off): the next item blinks for about a second before you speak.

**Auto-redo** (on). After each take, if it looks bad it is **not** marked done: the item turns amber and asks you to say it again.
"Bad" means too short (under 60 ms), too long (sounds and words over 3 s, sentences over 12 s), too quiet (under −50 dB) or clipping
(peak over −0.3 dB). All five numbers are options. It asks at most twice per item; the third take is kept with a warning so you are never stuck.
Hold-to-record takes are never judged.

**Listening**
* Silence that ends a take: sounds 500 ms, words 700 ms, sentences **auto**: 1000 ms for short ones, +80 ms for every word above 6, never
  more than 2000 ms. Or give sentences one fixed number.
* **Detection level** above the room noise (12 dB) and how much each **Stop & retry** lowers it (3 dB).

**Files** (apply to files you make from now on; press **Rebuild all files** on the lists screen to redo old ones)
* Quiet kept each side `pad_s` 0.12 s, `fade_ms` 10, loudness `target_rms_db` −20, loudest peak `peak_db` −1.
* Quality `aac_kbps` 64 (try 96 or 128 if short sounds like /k/ and /t/ sound rough).
* Sound said `sound_repeats` 3 times, `sound_gap_s` 0.6 between repeats, `version_pause_s` 1.0 between version 1 and 2.

**Microphone reminder:** the text on the quiet-check screen.

Only options that differ from the default are written to `settings.json`. The one other thing in that file is `website_folder`
(default `~/Documents/212english-website`, where the CSV lists are read from; the studio only **reads** there). See `settings.example.json`.

## The lists and their files

| List | Items | You see | Files (in `output/phonics/`) |
|---|---|---|---|
| Sounds | 75 | the letters big, the key word small | `alone/<no>.m4a`, `alone/<no>-2.m4a`, `sounds/<no>.m4a` (see above) |
| Challenge words | 317 | the word big | `words/<word>.m4a` |
| Sentences | 213 | the sentence | `sentences/<id>.m4a` (for example `sentences/PS1.m4a`) |
| Common words | 825 | the word big | `words/<word>.m4a` |

Words and sentences are cut, levelled and named like sounds (one file each, no repeats). Progress follows the item key
(`word:cad`, `sentence:PS1`, `sound:23:v2`), never the list position, so changing a CSV later breaks nothing.
**Start over** and **Rebuild** (lists screen, "Tools for …") work per list, so redoing Challenge words never touches Common words.

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

Makes synthetic audio (tones and hiss with silences), runs the voice detector, the Redo/Skip logic, the option rules, the server and the whole
cut-clean-encode step for all four lists on it (decoding the `.m4a` files back to check lengths, gaps, loudness and bitrate), and says ALL TESTS PASSED. To watch the whole screen run on test audio instead of a microphone, start the studio
and open `http://localhost:8212/?testaudio=flow.wav&list=sounds&autostart=1` after running the tests once.
