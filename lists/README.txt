YOUR OWN LISTS
==============
Every file ending in .csv in this folder becomes a list in the studio (a card on the first screen).

The easy way: in the studio press "+ New list", type or paste one item per line, press "Make the list".
The studio writes the .csv file here for you.

By hand (Excel, Numbers, Google Sheets, or any text editor; save as CSV, UTF-8):

    name,text
    apple,apple
    red-pepper,red pepper
    hello,Hello! How are you today?

  text   what you will read on the screen (required)
  name   the file name of the finished sound: letters, numbers, - and _ only (optional; made from the text if empty)

Tips
- Keep a name the same once you have recorded it. The studio remembers your progress by name, so
  changing the text is fine but changing the name makes it a new item.
- If most items have 4 or more words, the list behaves like Sentences: press Enter when you finish an item.
  Otherwise it behaves like words (a short pause ends the item by itself).
- A file whose name starts with _ or . is ignored. Copy _example.csv, rename it (no underscore) and edit it.
- Finished sounds go in the output/lists/<list name>/ folder (or the folder you chose in settings.json).
