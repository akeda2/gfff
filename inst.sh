#!/bin/bash

sudo install -m 755 rsb.sh /usr/local/bin/rsb && echo "rsb install SUCCESS!" || echo "rsb install FAIL!"
sudo install -m 755 gfff.sh /usr/local/bin/gfff && echo "gfff install SUCCESS!" || echo "gfff install FAIL!"
#sudo install -m 755 sockt.sh /usr/local/bin/sockt && echo "sockt install SUCCESS!" || echo "sockt install FAIL!"