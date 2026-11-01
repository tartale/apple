-- Prints one line per library in PowerPhotos: its name, a tab, and the path of the library on disk.
-- Usage: osascript libraries.applescript
-- Names and locations are fetched for all libraries at once, and a location is not coerced to an alias unless it must be:
-- resolving an alias checks the path on disk, which is slow for libraries on a network share.
tell application "PowerPhotos"
	set theNames to name of every library
	set theLocations to location of every library
end tell
set output to ""
repeat with i from 1 to count of theNames
	set aLocation to item i of theLocations
	try
		set thePath to POSIX path of aLocation
	on error
		set thePath to POSIX path of (aLocation as alias)
	end try
	set output to output & (item i of theNames) & tab & thePath & linefeed
end repeat
return output
