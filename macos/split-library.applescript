-- Copy one media type (photos or videos) of the System Photo Library into one library per year, using PowerPhotos.
-- Usage: osascript macos/split-library.applescript <photos|videos> <first year> <last year> <name of the library to split>
-- Each year goes into a library named "<photos|videos>-<year>" that already exists in PowerPhotos.
-- Photos already in the target library are skipped, so reruns add only what is new. Nothing is removed from the source.

property photoExtensions : {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw"}
property videoExtensions : {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mpg", ".mpeg"}

on run argv
	if (count of argv) is not 4 then error "Usage: osascript split-library.applescript <photos|videos> <first year> <last year> <library name>"
	set kind to item 1 of argv
	set firstYear to (item 2 of argv) as integer
	set lastYear to (item 3 of argv) as integer
	set sourceName to item 4 of argv
	if kind is "photos" then
		set extensions to photoExtensions
	else if kind is "videos" then
		set extensions to videoExtensions
	else
		error "The first argument must be photos or videos, not " & kind
	end if

	set report to ""
	tell application "PowerPhotos"
		set sourceLibrary to library sourceName
		repeat with y from firstYear to lastYear
			set targetName to kind & "-" & y
			try
				set targetLibrary to library targetName
			on error
				error "Create an empty library named " & targetName & " in PowerPhotos first (the script cannot create one)."
			end try
			set startDate to my dateFor(y, 1, 1)
			set endDate to my dateFor(y + 1, 1, 1)
			set wanted to {}
			repeat with ext in extensions
				set wanted to wanted & (photos of sourceLibrary whose date ≥ startDate and date < endDate and filename ends with (contents of ext))
			end repeat
			if (count of wanted) > 0 then duplicate wanted to targetLibrary with skip duplicates
			set report to report & targetName & ": " & (count of wanted) & " found" & linefeed
		end repeat
	end tell
	return report
end run

on dateFor(y, m, d)
	set theDate to current date
	set day of theDate to 1
	set year of theDate to y
	set month of theDate to m
	set day of theDate to d
	set time of theDate to 0
	return theDate
end dateFor
