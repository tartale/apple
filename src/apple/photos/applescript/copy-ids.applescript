-- Copies the photos of one PowerPhotos library whose Photos identifiers are listed in a text file into another library.
-- Usage: osascript copy-ids.applescript <source library> <destination library> <file with one identifier per line> <true|false>
-- An identifier is the UUID that starts the id PowerPhotos gives a photo (<UUID>/L0/001), which is Photos' own identifier
-- for it and names exactly one photo, whatever its file name. The last argument says whether to skip photos the destination
-- already has (true) or copy every listed photo again (false); both are passed to PowerPhotos explicitly, since the default
-- of its "skip duplicates" parameter when left out is not documented. Nothing is removed from the source.
-- The photos are looked up by PowerPhotos with a filter (every photo whose id is "a" or id is "b" ...), in batches of
-- chunkSize, so that a large source library is not read photo by photo here. Prints "missing<tab><identifier>" for each
-- listed identifier that no source photo has (PowerPhotos does not list hidden items), then "found <n>". Failures (such as
-- a destination that needs a Photos upgrade, error 5) are left to osascript to report.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day
property chunkSize : 50 -- identifiers to a lookup
property foundTotal : 0

on run argv
	set sourceName to item 1 of argv
	set destinationName to item 2 of argv
	set checkDuplicates to (item 4 of argv) is "true"
	set listedIds to {}
	repeat with anId in paragraphs of (read (POSIX file (item 3 of argv)) as «class utf8»)
		if contents of anId is not "" then set end of listedIds to contents of anId
	end repeat

	set present to linefeed
	set foundTotal to 0
	set batch to {}
	repeat with anId in listedIds
		set end of batch to contents of anId
		if (count of batch) is chunkSize then
			set present to present & my copyBatch(sourceName, destinationName, checkDuplicates, batch)
			set batch to {}
		end if
	end repeat
	if (count of batch) > 0 then set present to present & my copyBatch(sourceName, destinationName, checkDuplicates, batch)

	set report to ""
	repeat with anId in listedIds
		if present does not contain (linefeed & (contents of anId) & linefeed) then set report to report & "missing" & tab & (contents of anId) & linefeed
	end repeat
	return report & "found " & foundTotal
end run

-- Copies the photos with these identifiers; returns the identifiers it found, one to a line.
on copyBatch(sourceName, destinationName, checkDuplicates, uuids)
	set tests to ""
	repeat with anId in uuids
		if tests is not "" then set tests to tests & " or "
		set tests to tests & "id is " & my asLiteral((contents of anId) & "/L0/001")
	end repeat
	if checkDuplicates then
		set skipping to "with skip duplicates"
	else
		set skipping to "without skip duplicates"
	end if
	set filterText to "(every photo of library " & my asLiteral(sourceName) & " whose " & tests & ")"
	set foundIds to run script "with timeout of " & timeoutSeconds & " seconds" & linefeed & "tell application \"PowerPhotos\"" & linefeed & "set foundIds to id of " & filterText & linefeed & "if (count of foundIds) > 0 then duplicate " & filterText & " to library " & my asLiteral(destinationName) & " " & skipping & linefeed & "return foundIds" & linefeed & "end tell" & linefeed & "end timeout"
	set foundLines to ""
	repeat with aFoundId in foundIds
		set foundLines to foundLines & my uuidOf(contents of aFoundId) & linefeed
		set foundTotal to foundTotal + 1
	end repeat
	return foundLines
end copyBatch

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
