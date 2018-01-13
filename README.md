# Stud.IP API

_studip-api_ provides a python3 API to courses and files hosted on the course management tool Stud.IP.

_studip-api_ works by crawling the Stud.IP web interface and will therefore ask for your username and password.
All connections to the university servers transporting the login data are made via HTTPS.
Your credentials will not be copied or distributed in any other way.

_studip-api_ is used by [_studip-fuse_](https://github.com/N-Coder/studip-fuse),
a FUSE (file-system in user-space) driver that provides files from lectures in the course management tool Stud.IP on your computer.
