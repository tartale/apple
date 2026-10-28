-- Prints one line per library in PowerPhotos: its name, a tab, and the path of the library on disk.
-- Usage: osascript libraries.applescript
tell application "PowerPhotos"
	set output to ""
	repeat with aLibrary in every library
		set output to output & (name of aLibrary) & tab & (POSIX path of ((location of aLibrary) as alias)) & linefeed
	end repeat
	return output
end tell
