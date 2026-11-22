"""Space Finder - launch the app:  python main.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spacefinder.gui import main

if __name__ == "__main__":
    main()
