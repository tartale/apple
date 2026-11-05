-- Imports files into the library Photos has open.
-- Usage: osascript import-photos.applescript <file with one file path per line>
-- Photos skips files it already has. Prints "imported <n>". Failures are left to osascript to report.
-- (Variable names avoid AppleScript's own words: "files" would mean every file.)

property timeoutSeconds : 3600 -- how long to wait for the import

on run argv
	set pathLines to paragraphs of (read (POSIX file (item 1 of argv)) as «class utf8»)
	set toImport to {}
	repeat with aLine in pathLines
		if (contents of aLine) is not "" then set end of toImport to POSIX file (contents of aLine)
	end repeat
	if (count of toImport) is 0 then return "imported 0"
	with timeout of timeoutSeconds seconds
		tell application "Photos"
			set newItems to import toImport skip check duplicates false
		end tell
	end timeout
	return "imported " & (count of newItems)
end run
