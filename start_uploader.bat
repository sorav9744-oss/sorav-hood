@echo off
rem Starts the Shorts uploader page in the background at Windows login (no window). It also fills the buffer by itself.
rem Page: http://localhost:8790/   (add --open below to also open the browser)
cd /d "c:\Users\prajyot\Documents\Youtube\clippps"
start "" "C:\Users\prajyot\AppData\Local\Programs\Python\Python311\pythonw.exe" -m joshiclip.pcapp
