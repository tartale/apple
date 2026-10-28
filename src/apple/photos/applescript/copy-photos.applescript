-- Copies the photos and videos of one PowerPhotos library that match a date range and media type into another library.
-- Usage: osascript copy-photos.applescript <source library> <destination library> <since> <before> <photos|videos|all>
-- since is the first date to copy and before the first date not to copy, both as yyyy-mm-dd, or - for no limit.
-- Photos already in the destination are skipped, so reruns add only what is new. Nothing is removed from the source.
-- Prints "found <n>". Failures (such as a destination that needs a Photos upgrade, error 5) are left to osascript to report.

property photoExtensions : {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw"}
property videoExtensions : {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mpg", ".mpeg"}
property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day

on run argv
	set sourceName to item 1 of argv
	set destinationName to item 2 of argv
	set sinceDate to my parseDate(item 3 of argv)
	set beforeDate to my parseDate(item 4 of argv)
	set mediaKind to item 5 of argv
	if mediaKind is "photos" then
		set extensionList to photoExtensions
	else if mediaKind is "videos" then
		set extensionList to videoExtensions
	else
		set extensionList to {""} -- no filter on the file name
	end if

	set wanted to {}
	with timeout of timeoutSeconds seconds
		tell application "PowerPhotos"
			set sourceLibrary to library sourceName
			set destinationLibrary to library destinationName
			repeat with anExtension in extensionList
				set wanted to wanted & (my matching(sourceLibrary, sinceDate, beforeDate, contents of anExtension))
			end repeat
			if (count of wanted) > 0 then duplicate wanted to destinationLibrary with skip duplicates
		end tell
	end timeout
	return "found " & (count of wanted)
end run

-- The photos of a library in the date range whose file name ends with anExtension ("" for any file name)
on matching(sourceLibrary, sinceDate, beforeDate, anExtension)
	tell application "PowerPhotos"
		if anExtension is "" then
			if sinceDate is missing value and beforeDate is missing value then return (get photos of sourceLibrary)
			if beforeDate is missing value then return (get photos of sourceLibrary whose (its date ≥ sinceDate))
			if sinceDate is missing value then return (get photos of sourceLibrary whose (its date < beforeDate))
			return (get photos of sourceLibrary whose (its date ≥ sinceDate) and (its date < beforeDate))
		end if
		if sinceDate is missing value and beforeDate is missing value then return (get photos of sourceLibrary whose (its filename ends with anExtension))
		if beforeDate is missing value then return (get photos of sourceLibrary whose (its date ≥ sinceDate) and (its filename ends with anExtension))
		if sinceDate is missing value then return (get photos of sourceLibrary whose (its date < beforeDate) and (its filename ends with anExtension))
		return (get photos of sourceLibrary whose (its date ≥ sinceDate) and (its date < beforeDate) and (its filename ends with anExtension))
	end tell
end matching

on parseDate(isoText)
	if isoText is "-" then return missing value
	set AppleScript's text item delimiters to "-"
	set parts to text items of isoText
	set AppleScript's text item delimiters to ""
	set theDate to current date
	set day of theDate to 1
	set year of theDate to (item 1 of parts) as integer
	set month of theDate to (item 2 of parts) as integer
	set day of theDate to (item 3 of parts) as integer
	set time of theDate to 0
	return theDate
end parseDate
