-- Imports files into the library Photos has open.
-- Usage: osascript import-photos.applescript <file with one file path per line>
-- Photos skips files it already has. Prints "imported <n>". Failures are left to osascript to report.

property timeoutSeconds : 3600 -- how long to wait for the import

on run argv
	set paths to paragraphs of (read (POSIX file (item 1 of argv)) as «class utf8»)
	set files to {}
	repeat with aPath in paths
		if (contents of aPath) is not "" then set end of files to POSIX file (contents of aPath)
	end repeat
	if (count of files) is 0 then return "imported 0"
	with timeout of timeoutSeconds seconds
		tell application "Photos"
			set imported to import files skip check duplicates false
		end tell
	end timeout
	return "imported " & (count of imported)
end run
