#!/bin/bash

sudo install -m 755 rsb.sh /usr/local/bin/rsb && echo "rsb install SUCCESS!" || echo "rsb install FAIL!"
sudo install -m 755 gfff.sh /usr/local/bin/gfff && echo "gfff install SUCCESS!" || echo "gfff install FAIL!"
sudo install -m 755 wrsb.sh /usr/local/bin/wrsb && echo "wrsb install SUCCESS!" || echo "wrsb install FAIL!"
sudo install -m 755 ytdlpb.sh /usr/local/bin/ytdlpb && echo "ytdlpb install SUCCESS!" || echo "ytdlpb install FAIL!"