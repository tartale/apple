-- Copy one media type (photos or videos) of the System Photo Library into one library per year, using PowerPhotos.
-- Usage: osascript macos/split-library.applescript <photos|videos> <first year> <last year> <name of the library to split> [skip|ask|stop]
-- The last argument says what to do when a target library needs upgrading by Photos before it accepts photos. That is checked
-- before any copying, by comparing each target's Photos database version with the source's, and again if a copy is refused:
--   skip (default)  leave that year out, say so in the report and go on (for unattended runs)
--   ask             show PowerPhotos' message in a dialog: Skip, Stop, or open the library in Photos to upgrade it, then Retry
--   stop            end the run with the error
-- Each year goes into a library named "<photos|videos>-<year>" that already exists in PowerPhotos.
-- Photos already in the target library are skipped, so reruns add only what is new. Nothing is removed from the source.

property photoExtensions : {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw"}
property videoExtensions : {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mpg", ".mpeg"}

on run argv
	if (count of argv) < 4 or (count of argv) > 5 then error "Usage: osascript split-library.applescript <photos|videos> <first year> <last year> <library name> [skip|ask|stop]"
	set mediaKind to item 1 of argv
	set firstYear to (item 2 of argv) as integer
	set lastYear to (item 3 of argv) as integer
	set sourceName to item 4 of argv
	set onUpgrade to "skip"
	if (count of argv) is 5 then set onUpgrade to item 5 of argv
	if onUpgrade is not in {"skip", "ask", "stop"} then error "The last argument must be skip, ask or stop, not " & onUpgrade
	if mediaKind is "photos" then
		set mediaExtensions to photoExtensions
	else if mediaKind is "videos" then
		set mediaExtensions to videoExtensions
	else
		error "The first argument must be photos or videos, not " & mediaKind
	end if

	set report to ""
	tell application "PowerPhotos"
		try
			set sourceLibrary to library sourceName
		on error
			error "No library named " & sourceName & " in PowerPhotos. Libraries: " & my joined(name of every library)
		end try
		-- Before any slow work: a target whose Photos database is older than the source's needs an upgrade by Photos
		-- (the copy would fail with error 5). A library that cannot be read is left to that error.
		set sourceVersion to my modelVersion(location of sourceLibrary)
		set oldYears to {}
		if sourceVersion is not missing value then
			repeat with y from firstYear to lastYear
				try
					set targetVersion to my modelVersion(location of library (mediaKind & "-" & y))
					if targetVersion is not missing value and targetVersion < sourceVersion then set end of oldYears to y
				end try
			end repeat
		end if
		if (count of oldYears) > 0 and onUpgrade is "stop" then error "These target libraries are in an older format than " & sourceName & " and need a Photos upgrade: " & my joined(oldYears)

		repeat with y from firstYear to lastYear
			set targetName to mediaKind & "-" & y
			try
				set targetLibrary to library targetName
			on error
				error "Create an empty library named " & targetName & " in PowerPhotos first (the script cannot create one). Libraries: " & my joined(name of every library)
			end try
			set skipYear to false
			if oldYears contains y then
				if onUpgrade is "skip" then
					set skipYear to true
				else
					set answer to my askAboutUpgrade(targetName & " is in an older format than " & sourceName & ". Photos must upgrade it before photos can be copied into it.")
					if answer is "Stop" then error targetName & " needs a Photos upgrade."
					if answer is "Skip" then set skipYear to true
					if answer is "Open in Photos" then
						open targetLibrary
						if my askToRetry(targetName) is "Skip" then set skipYear to true
					end if
				end if
			end if
			if skipYear then
				set report to report & targetName & ": skipped (the library needs an upgrade)" & linefeed
			else
				set startDate to my dateFor(y, 1, 1)
				set endDate to my dateFor(y + 1, 1, 1)
				set wanted to {}
				repeat with ext in mediaExtensions
					set wanted to wanted & (photos of sourceLibrary whose (its date ≥ startDate) and (its date < endDate) and (its filename ends with (contents of ext)))
				end repeat
				set outcome to ""
				if (count of wanted) > 0 then
					repeat
						try
							duplicate wanted to targetLibrary with skip duplicates
							exit repeat
						on error message_ number errorNumber
							-- 5 is "Library needs upgrade"; anything else is a real failure
							if errorNumber is not 5 or onUpgrade is "stop" then error message_ number errorNumber
							set answer to "Skip"
							if onUpgrade is "ask" then set answer to my askAboutUpgrade(message_)
							if answer is "Stop" then error message_ number errorNumber
							if answer is "Skip" then
								set outcome to " (skipped: the library needs an upgrade)"
								exit repeat
							end if
							open targetLibrary
							set answer to my askToRetry(targetName)
							if answer is "Skip" then
								set outcome to " (skipped: the library needs an upgrade)"
								exit repeat
							end if
						end try
					end repeat
				end if
				set report to report & targetName & ": " & (count of wanted) & " found" & outcome & linefeed
			end if
		end repeat
	end tell
	return report
end run

on askAboutUpgrade(message_)
	set answer to button returned of (display dialog message_ buttons {"Skip", "Stop", "Open in Photos"} default button "Skip" with icon caution)
	return answer
end askAboutUpgrade

on askToRetry(libraryName)
	set answer to button returned of (display dialog "Upgrade " & libraryName & " in Photos, quit Photos, then click Retry." buttons {"Skip", "Retry"} default button "Retry")
	return answer
end askToRetry

-- The Photos database version (PLModelVersion) of a library, or missing value when it cannot be read
on modelVersion(loc)
	try
		set libPath to POSIX path of (loc as alias)
		if libPath does not end with "/" then set libPath to libPath & "/"
		set versionText to do shell script "sqlite3 -readonly " & quoted form of (libPath & "database/Photos.sqlite") & " 'select hex(Z_PLIST) from Z_METADATA' | xxd -r -p | plutil -extract PLModelVersion raw -o - -"
		return versionText as integer
	on error
		return missing value
	end try
end modelVersion

on joined(names)
	set AppleScript's text item delimiters to ", "
	set joinedNames to names as text
	set AppleScript's text item delimiters to ""
	return joinedNames
end joined

on dateFor(y, m, d)
	set theDate to current date
	set day of theDate to 1
	set year of theDate to y
	set month of theDate to m
	set day of theDate to d
	set time of theDate to 0
	return theDate
end dateFor
