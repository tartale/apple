-- Moves the photos of one PowerPhotos library whose Photos identifiers are listed in a text file to its Recently Deleted album.
-- Usage: osascript trash-ids.applescript <library> <file with one identifier per line>
-- An identifier is the UUID at the start of the id PowerPhotos gives a photo (<UUID>/L0/001), which names exactly one
-- photo. PowerPhotos' delete moves photos to Recently Deleted; nothing is removed from disk. Prints "missing<tab><identifier>"
-- for each listed identifier that no photo of the library has (PowerPhotos does not list hidden items), then "deleted <n>".
-- Failures are left to osascript to report.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day

on run argv
	set libraryName to item 1 of argv
	set listedIds to {}
	repeat with anId in paragraphs of (read (POSIX file (item 2 of argv)) as «class utf8»)
		if contents of anId is not "" then set end of listedIds to contents of anId
	end repeat
	set listedText to linefeed & (my joined(listedIds)) & linefeed

	set toDelete to {}
	set present to linefeed
	with timeout of timeoutSeconds seconds
		tell application "PowerPhotos"
			set theLibrary to library libraryName
			set allPhotos to photos of theLibrary
			set allIds to id of photos of theLibrary
			repeat with i from 1 to count of allIds
				set anId to my uuidOf(item i of allIds)
				if listedText contains (linefeed & anId & linefeed) then
					set end of toDelete to item i of allPhotos
					set present to present & anId & linefeed
				end if
			end repeat
			if (count of toDelete) > 0 then delete toDelete
		end tell
	end timeout

	set report to ""
	repeat with anId in listedIds
		if present does not contain (linefeed & (contents of anId) & linefeed) then set report to report & "missing" & tab & (contents of anId) & linefeed
	end repeat
	return report & "deleted " & (count of toDelete)
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
