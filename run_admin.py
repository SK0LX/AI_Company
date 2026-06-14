"""Deprecated alias — the admin panel and bots now run as ONE process.

Use ``python main.py`` instead. Kept so existing muscle memory still works; it
simply launches the unified entry point.
"""
from main import main

if __name__ == "__main__":
    main()
