# 212 Recording Studio: start here

Record your own voice reading a list (words, phrases, sentences). You read from a big scrolling screen, one long recording.
The studio finds each item, cuts it out, evens out the loudness and saves one sound file per item, named for you.
No editing. Works on a **Mac**.

## What you need
* A Mac, and Chrome or Safari.
* A microphone (the Mac's own works; a USB mic is better).
* **Python 3** (a free program). Don't worry: the first time you start, the studio checks and tells you what to do.

## Start it (every time)
1. Open the studio folder.
2. **Double-click `start.command`.** A Terminal window opens (leave it open; closing it stops the studio).
3. Your browser opens the studio by itself. If not, go to **http://localhost:8212**.

### The first time only
* **"cannot be opened because it is from an unidentified developer"**: right-click `start.command`, choose **Open**, then **Open** again.
* **"permission denied"**: in Terminal type `chmod +x ` (with a space at the end), drag `start.command` into the Terminal window, press Return, and double-click again.
* **It says Python is missing**: install it from https://www.python.org/downloads/ , then double-click `start.command` again.
* **It says numpy is missing** and asks `Set it up now? (y/n)`: type `y` and press Return. It makes a private folder called `.venv` inside the
  studio folder and installs one small helper (numpy, about 15 MB, needs the internet). Nothing else on your Mac changes.
* The browser asks **"Allow this page to use the microphone?"**: click **Allow**.

## Make a list
1. On the first screen press **＋ New list**.
2. Give it a name, then type or paste what you want to read: **one item per line**. Press **Make the list**.
3. The list appears as a card. (It is saved as a file in the `lists` folder, so you can also make lists in Excel or Numbers: see `lists/README.txt`.)

## Record
1. Click your list's card. **Stay quiet for 2 seconds** while the studio listens to the room.
2. Press **Start recording** and read the big line. Pause a moment, then read the next. The studio notices when you finish and moves on by itself.
3. Short items: a short pause moves to the next. **Long items** (most lines have 4+ words): press **Enter** when you finish a line.
4. Made a mistake? Press **←** (Redo) and say it again, or click any earlier line to redo that one. **→** skips. **Space** pauses.
5. Press **Finish** when you want to stop. Your place is saved: next time the list starts where you stopped.

Your recording is saved every 30 seconds, so a crash loses almost nothing.

## Check and find your files
* After **Finish** the studio cuts everything and shows a **check screen**: press ▶ to listen to each file, then **OK** or **Redo**.
  **Redo** puts that item back in the list to record again.
* Press **Show my finished files** on the first screen (or **Show in Finder** on the check screen) to open the folder.
  Your sound files are in `output/lists/<your list name>/`, one `.m4a` per item, named after the text (for example `apple.m4a`).
* Want them somewhere else (your Desktop, a drive)? Press **⚙ Options**, find **Where files go**, press **Choose folder…**, then **Save**.

## If something goes wrong
| What you see | What to do |
|---|---|
| The page says the studio is not answering | The Terminal window was closed. Double-click `start.command` again. |
| "Port 8212 is busy" / it opens but acts strange | The studio is already running in another Terminal window. Use that one, or close it and start again. |
| The meter does not move when you speak | Browser menu → Site settings → Microphone: allow it. Check the right microphone is chosen on the quiet-check screen. |
| Items are cut too early or missed | Open **⚙ Options** and try **Noise suppression** off, or move the mic closer. **Stop & retry** (R) makes it listen more sensitively. |
| Sounds are too quiet or too loud | They are levelled automatically. If a file is marked "clipped", turn the mic's gain down and Redo it. |
| Nothing at all works | Close the Terminal window, double-click `start.command` again, and tell Mike what the window says. |

**Nothing is ever deleted** by the studio. "Start over" moves things into `takes/_old`.
