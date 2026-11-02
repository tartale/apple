-- Copies the photos and videos of one PowerPhotos library whose file names are listed in a text file into another library.
-- Usage: osascript copy-named.applescript <source library> <destination library> <file with one file name per line> <true|false>
-- The last argument says whether to skip photos the destination already has (true: reruns add only what is new; false:
-- every listed photo is copied again). Both are passed to PowerPhotos explicitly, since the default of its
-- "skip duplicates" parameter when left out is not documented. Nothing is removed from the source.
-- Every source photo with a listed file name is copied. Prints "missing<tab><name>" for each listed name that no source
-- photo has, then "found <n>". Failures (such as a destination that needs a Photos upgrade, error 5) are left to
-- osascript to report.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day

on run argv
	set sourceName to item 1 of argv
	set destinationName to item 2 of argv
	set checkDuplicates to (item 4 of argv) is "true"
	set listedNames to paragraphs of (read (POSIX file (item 3 of argv)) as «class utf8»)
	set listedText to linefeed & (my joined(listedNames)) & linefeed

	set wanted to {}
	set present to linefeed
	with timeout of timeoutSeconds seconds
		tell application "PowerPhotos"
			set sourceLibrary to library sourceName
			set destinationLibrary to library destinationName
			set allPhotos to photos of sourceLibrary
			set allNames to filename of photos of sourceLibrary
			repeat with i from 1 to count of allNames
				set aName to item i of allNames
				if listedText contains (linefeed & aName & linefeed) then
					set end of wanted to item i of allPhotos
					if present does not contain (linefeed & aName & linefeed) then set present to present & aName & linefeed
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
	repeat with aName in listedNames
		if (contents of aName) is not "" and present does not contain (linefeed & aName & linefeed) then set report to report & "missing" & tab & aName & linefeed
	end repeat
	return report & "found " & (count of wanted)
end run

on joined(theList)
	set AppleScript's text item delimiters to linefeed
	set joinedText to theList as text
	set AppleScript's text item delimiters to ""
	return joinedText
end joined
