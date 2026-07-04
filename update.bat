@echo off
git add .
git commit -m "auto update - %date% %time%"
git push
echo Update completed!
pause