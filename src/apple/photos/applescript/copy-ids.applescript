-- Copies the photos of one PowerPhotos library whose Photos identifiers are listed in a text file into another library.
-- Usage: osascript copy-ids.applescript <source library> <destination library> <file with one identifier per line> <true|false>
-- An identifier is the UUID that starts the id PowerPhotos gives a photo (<UUID>/L0/001), which is Photos' own identifier
-- for it and names exactly one photo, whatever its file name. The last argument says whether to skip photos the destination
-- already has (true) or copy every listed photo again (false); both are passed to PowerPhotos explicitly, since the default
-- of its "skip duplicates" parameter when left out is not documented. Nothing is removed from the source.
-- Each photo is reached by its id (photo id "<UUID>/L0/001" of library ...), which PowerPhotos looks up directly: a filter
-- (every photo whose id is ...) or a loop over the library makes it examine every photo of a large source library, and
-- the copy looks hung. Prints "missing<tab><identifier>" for each listed identifier that no source photo has (PowerPhotos
-- does not list hidden items), then "found <n>". Failures (such as a destination that needs a Photos upgrade, error 5) are
-- left to osascript to report.

property timeoutSeconds : 86400 -- how long to wait for one PowerPhotos command: a day

on run argv
	set sourceName to item 1 of argv
	set destinationName to item 2 of argv
	set checkDuplicates to (item 4 of argv) is "true"
	set listedIds to {}
	repeat with anId in paragraphs of (read (POSIX file (item 3 of argv)) as «class utf8»)
		if contents of anId is not "" then set end of listedIds to contents of anId
	end repeat

	set wanted to {}
	set report to ""
	with timeout of timeoutSeconds seconds
		tell application "PowerPhotos"
			set sourceLibrary to library sourceName
			set destinationLibrary to library destinationName
			repeat with anId in listedIds
				try
					set aPhoto to photo id ((contents of anId) & "/L0/001") of sourceLibrary
					set resolvedId to id of aPhoto -- fails here, not later, for an id the library does not have
					set end of wanted to aPhoto
				on error
					set report to report & "missing" & tab & (contents of anId) & linefeed
				end try
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
	return report & "found " & (count of wanted)
end run
