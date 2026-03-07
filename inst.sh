#!/bin/bash

sudo install -m 755 rsb.sh /usr/local/bin/rsb && echo "rsb install SUCCESS!" || echo "rsb install FAIL!"
sudo install -m 755 gfff.sh /usr/local/bin/gfff && echo "gfff install SUCCESS!" || echo "gfff install FAIL!"
sudo install -m 755 wrsb.sh /usr/local/bin/wrsb && echo "wrsb install SUCCESS!" || echo "wrsb install FAIL!"
sudo install -m 755 buildbot.py /usr/local/bin/gfff-buildbot && echo "gfff-buildbot install SUCCESS!" || echo "gfff-buildbot install FAIL!"
