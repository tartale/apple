-- Copies the photos of one PowerPhotos library whose Photos identifiers are listed in a text file into another library.
-- Usage: osascript copy-ids.applescript <source library> <destination library> <file with one identifier per line> <true|false>
-- An identifier is the UUID at the start of the id PowerPhotos gives a photo (<UUID>/L0/001), which is Photos' own
-- identifier for it and names exactly one photo, whatever its file name. The last argument says whether to skip photos the
-- destination already has (true) or copy every listed photo again (false); both are passed to PowerPhotos explicitly, since
-- the default of its "skip duplicates" parameter when left out is not documented. Nothing is removed from the source.
-- Prints "missing<tab><identifier>" for each listed identifier that no source photo has (PowerPhotos does not list hidden
-- items), then "found <n>". Failures (such as a destination that needs a Photos upgrade, error 5) are left to osascript.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day

on run argv
	set sourceName to item 1 of argv
	set destinationName to item 2 of argv
	set checkDuplicates to (item 4 of argv) is "true"
	set listedIds to {}
	repeat with anId in paragraphs of (read (POSIX file (item 3 of argv)) as «class utf8»)
		if contents of anId is not "" then set end of listedIds to contents of anId
	end repeat
	set listedText to linefeed & (my joined(listedIds)) & linefeed

	set wanted to {}
	set present to linefeed
	with timeout of timeoutSeconds seconds
		tell application "PowerPhotos"
			set sourceLibrary to library sourceName
			set destinationLibrary to library destinationName
			set allPhotos to photos of sourceLibrary
			set allIds to id of photos of sourceLibrary
			repeat with i from 1 to count of allIds
				set anId to my uuidOf(item i of allIds)
				if listedText contains (linefeed & anId & linefeed) then
					set end of wanted to item i of allPhotos
					set present to present & anId & linefeed
				end if
			end repeat
			if (count of wanted) > 0 then
				if checkDuplicates then
					duplicate wanted to destinationLibrary with skip duplicates
				else
					duplicate wanted to destinationLibrary without skip duplicates
				end if
			end if
		end tell
	end timeout

	set report to ""
	repeat with anId in listedIds
		if present does not contain (linefeed & (contents of anId) & linefeed) then set report to report & "missing" & tab & (contents of anId) & linefeed
	end repeat
	return report & "found " & (count of wanted)
end run

on uuidOf(photoId)
	set AppleScript's text item delimiters to "/"
	set theUuid to text item 1 of photoId
	set AppleScript's text item delimiters to ""
	return theUuid
end uuidOf

on joined(theList)
	set AppleScript's text item delimiters to linefeed
	set joinedText to theList as text
	set AppleScript's text item delimiters to ""
	return joinedText
end joined
