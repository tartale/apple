-- Moves the photos of one PowerPhotos library whose Photos identifiers are listed in a text file to its Recently Deleted album.
-- Usage: osascript trash-ids.applescript <library> <file with one identifier per line>
-- An identifier is the UUID at the start of the id PowerPhotos gives a photo (<UUID>/L0/001), which names exactly one
-- photo. PowerPhotos' delete moves photos to Recently Deleted; nothing is removed from disk. Prints "missing<tab><identifier>"
-- for each listed identifier that no photo of the library has (PowerPhotos does not list hidden items), then "deleted <n>".
-- Failures are left to osascript to report.
-- PowerPhotos' delete takes one specifier, not a list of photos, and asks for confirmation each time it is called. A filter
-- that names several photos (every photo whose id is "a" or id is "b" ...) is one specifier, so the photos are deleted
-- in batches of chunkSize with one confirmation each, using a script built for each batch.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day
property chunkSize : 100 -- photos to a delete, and so to a confirmation

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
			set allIds to id of photos of theLibrary
			repeat with i from 1 to count of allIds
				set anId to my uuidOf(item i of allIds)
				if listedText contains (linefeed & anId & linefeed) then
					set end of toDelete to item i of allIds
					set present to present & anId & linefeed
				end if
			end repeat
		end tell
	end timeout

	set batch to {}
	repeat with anId in toDelete
		set end of batch to contents of anId
		if (count of batch) is chunkSize then
			my deleteBatch(libraryName, batch)
			set batch to {}
		end if
	end repeat
	if (count of batch) > 0 then my deleteBatch(libraryName, batch)

	set report to ""
	repeat with anId in listedIds
		if present does not contain (linefeed & (contents of anId) & linefeed) then set report to report & "missing" & tab & (contents of anId) & linefeed
	end repeat
	return report & "deleted " & (count of toDelete)
end run

on deleteBatch(libraryName, fullIds)
	set tests to ""
	repeat with anId in fullIds
		if tests is not "" then set tests to tests & " or "
		set tests to tests & "id is " & my asLiteral(contents of anId)
	end repeat
	run script "with timeout of " & timeoutSeconds & " seconds" & linefeed & "tell application \"PowerPhotos\" to delete (every photo of library " & my asLiteral(libraryName) & " whose " & tests & ")" & linefeed & "end timeout"
end deleteBatch

on asLiteral(theText)
	return "\"" & my replacing(my replacing(theText, "\\", "\\\\"), "\"", "\\\"") & "\""
end asLiteral

on replacing(theText, target, replacement)
	set AppleScript's text item delimiters to target
	set pieces to text items of theText
	set AppleScript's text item delimiters to replacement
	set theText to pieces as text
	set AppleScript's text item delimiters to ""
	return theText
end replacing

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
