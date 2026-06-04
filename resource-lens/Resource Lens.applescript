-- Resource Lens.applescript — source for Resource Lens.app (a stay-open applet).
--
-- Built into the bundle with:
--   osacompile -s -o "Resource Lens.app" "Resource Lens.applescript"
-- (the build_app.sh helper does this and restores the icon/metadata).
--
-- Why an AppleScript applet: a bare shell script set as CFBundleExecutable is
-- not registered as a GUI app, so macOS won't show the Dock running indicator.
-- An applet IS a real app — it shows the dot, stays running, and supports Quit.
--
-- Design: the app itself runs in a visible Terminal window (start.command), in
-- the foreground, so you see live logs and Ctrl+C stops it. This applet just
-- supervises it: it stays open (Dock dot lit) while the server is up, quits
-- itself once the server stops, and kills the server if you Quit from the Dock.

property wasUp : false
property downTicks : 0

on run
	-- The project folder is the parent of this .app bundle.
	set appPath to POSIX path of (path to me)
	set projDir to do shell script "cd " & quoted form of appPath & "/.. && pwd -P"
	-- Open a Terminal window running the app (no Apple events → no Automation
	-- permission needed; `open` goes through LaunchServices).
	do shell script "open -a Terminal " & quoted form of (projDir & "/start.command")
end run

on reopen
	-- Clicking the Dock icon while already running brings the app's Terminal
	-- window to the front. `open -a Terminal` (no document) just activates
	-- Terminal via LaunchServices — no Apple events / Automation permission.
	try
		do shell script "open -a Terminal"
	end try
end reopen

on idle
	-- Watch port 5002. Once we've seen the server up and then gone (Ctrl+C or
	-- window closed), quit so the Dock indicator clears. Before it ever comes up
	-- (setup / login can take a while), keep waiting up to ~30 min.
	set isUp to (do shell script "lsof -tiTCP:5002 -sTCP:LISTEN >/dev/null 2>&1 && echo 1 || echo 0")
	if isUp is "1" then
		set wasUp to true
		set downTicks to 0
	else
		if wasUp then
			quit
		else
			set downTicks to downTicks + 1
			if downTicks > 600 then quit
		end if
	end if
	return 3
end idle

on quit
	-- Stop the server when quit from the Dock. Killing the listener on port 5002
	-- is the most reliable (that's only ever our server). Escalate to SIGKILL.
	try
		do shell script "P=$(lsof -tiTCP:5002 -sTCP:LISTEN 2>/dev/null); if [ -n \"$P\" ]; then kill $P 2>/dev/null; sleep 1; P=$(lsof -tiTCP:5002 -sTCP:LISTEN 2>/dev/null); [ -n \"$P\" ] && kill -9 $P 2>/dev/null; fi; true"
	end try
	continue quit
end quit
