-- Moves the photos of one PowerPhotos library whose file names are listed in a text file to the library's Recently Deleted album.
-- Usage: osascript trash-named.applescript <library> <file with one file name per line>
-- Each listed name must match exactly one photo of the library: a name no photo has prints "missing<tab><name>", and a
-- name more than one photo has prints "ambiguous<tab><name>", and neither is deleted. Names are compared without regard to
-- case, and .jpeg counts as .jpg because PowerPhotos reports a .jpeg file as .jpg. Prints "deleted <n>" last.
-- PowerPhotos' delete moves photos to Recently Deleted; nothing is removed from disk. Failures are left to osascript to report.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day

on run argv
	set libraryName to item 1 of argv
	set listedNames to {}
	set normalizedNames to {}
	repeat with aName in paragraphs of (read (POSIX file (item 2 of argv)) as «class utf8»)
		if contents of aName is not "" then
			set end of listedNames to contents of aName
			set end of normalizedNames to my normalized(contents of aName)
		end if
	end repeat
	set listedText to linefeed & (my joined(normalizedNames)) & linefeed

	set matchedNames to {}
	set matchedPhotos to {}
	set toDelete to {}
	set report to ""
	with timeout of timeoutSeconds seconds
		tell application "PowerPhotos"
			set theLibrary to library libraryName
			set allPhotos to photos of theLibrary
			set allNames to filename of photos of theLibrary
			repeat with i from 1 to count of allNames
				set aName to my normalized(item i of allNames)
				if listedText contains (linefeed & aName & linefeed) then
					set end of matchedNames to aName
					set end of matchedPhotos to item i of allPhotos
				end if
			end repeat
			repeat with i from 1 to count of normalizedNames
				set matches to my occurrences(item i of normalizedNames, matchedNames)
				if matches is 0 then
					set report to report & "missing" & tab & (item i of listedNames) & linefeed
				else if matches > 1 then
					set report to report & "ambiguous" & tab & (item i of listedNames) & linefeed
				else
					set end of toDelete to item (my firstIndex(item i of normalizedNames, matchedNames)) of matchedPhotos
				end if
			end repeat
			if (count of toDelete) > 0 then delete toDelete
		end tell
	end timeout
	return report & "deleted " & (count of toDelete)
end run

on occurrences(aName, names)
	set total to 0
	repeat with candidate in names
		if (contents of candidate) is aName then set total to total + 1
	end repeat
	return total
end occurrences

on firstIndex(aName, names)
	repeat with i from 1 to count of names
		if (item i of names) is aName then return i
	end repeat
	return 0
end firstIndex

on joined(theList)
	set AppleScript's text item delimiters to linefeed
	set joinedText to theList as text
	set AppleScript's text item delimiters to ""
	return joinedText
end joined

on normalized(fileName)
	if fileName ends with ".jpeg" then return (text 1 thru -6 of fileName) & ".jpg"
	return fileName
end normalized
