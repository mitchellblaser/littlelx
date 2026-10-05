-- littlelx companion for grandMA3.
--
-- Reports MA's state to the littlelx bridge over OSC: current page, fader
-- levels / names / running state of the executors on that page, the
-- Highlight/Lowlight/Solo/Blind states and the command line text.
--
-- Normally the BRIDGE INSTALLS AND STARTS THIS BY ITSELF (over OSC, using the
-- Lua keyword; see install_ma3() in bridge/littlelx.py). It can also be
-- imported as an ordinary plugin and tapped to start/stop.
--
-- Called with an argument it acts on MA's command line:
--   "type Store"   type a keyword (like pressing the key)
--   "please"       execute the command line
--   "clear"        clear the line, or Clear if it's empty
--   "back"         remove the last word
--   "page 3"       select executor page 3
--   "res Dimmer"   toggle MA's encoder resolution for an attribute (Coarse/Fine)
--   "__start <osc line> <tick> [Attr,Attr]"   start reporting from a Timer
--                  (bridge install); also report those attributes' resolution
--
-- MA needs an OSC line that SENDS to the bridge computer (its IP, port 9000,
-- Send = Yes). Its number is passed by the bridge; 2 if run as a plugin.
local OSC_LINE = 2

local WATCH = {}                    -- executors reported (buttons and faders)
for i = 101, 115 do WATCH[#WATCH + 1] = i end
for i = 201, 215 do WATCH[#WATCH + 1] = i end
local MASTERS = { "highlight", "lowlight", "solo", "blind" }
local ATTRS = { "Dimmer" }          -- attributes whose encoder resolution we report
local TICK = 0.1                    -- seconds between checks
local RUNVAR = "littlelx_running"
local GENVAR = "littlelx_gen"       -- bumps on every (re)start; old timers stop

local last = {}

-- MA3's tonumber only accepts strings: never hand it a number.
local function num(v)
	if type(v) == "number" then return v end
	if type(v) == "string" then return tonumber(v) end
	return nil
end

-- OSC strings travel inside SendOSC's quotes, separated by commas: keep
-- them out of the value.
local function clean(s)
	return (tostring(s or ""):gsub('"', "'"):gsub(",", ";"))
end

local function send(path, typ, value)
	if typ == "s" then
		value = clean(value)
	end
	Cmd(string.format('SendOSC %d "/littlelx/%s,%s,%s"', OSC_LINE, path, typ, tostring(value)))
end

local function changed(key, value)
	if last[key] == value then
		return false
	end
	last[key] = value
	return true
end

local function cmdtext()
	local ok, t = pcall(function() return CmdObj().CmdText end)
	return ok and t or ""
end

local function master_on(name)
	local ok, v = pcall(function() return MasterPool().Grand[name].FADERENABLED end)
	return ok and v and 1 or 0
end

-- MA's per-attribute encoder resolution (user profile) as a step factor:
-- Coarse 1, Fine 0.1, Increment 0.01 (MA stores these as 2^24-scaled numbers).
local RES_NAMES = { coarse = 1, fine = 0.1, increment = 0.01, ultra = 0.01 }
local function attr_prefs(attr)
	local ok, p = pcall(function() return CurrentProfile().UserAttributePreferences[attr] end)
	return ok and p or nil
end
local function resolution(attr)
	local p = attr_prefs(attr)
	if not p then return 1 end
	local ok, v = pcall(function() return p.EncoderResolution end)
	if not ok or v == nil then return 1 end
	if type(v) == "number" then
		if v <= 0 then return 1 end -- Default / Native: treat as coarse
		return math.floor(v / 16777216 * 1000 + 0.5) / 1000
	end
	return RES_NAMES[tostring(v):lower()] or 1
end
local function toggle_resolution(attr)
	local profile = "Default"
	pcall(function() profile = CurrentProfile().name or profile end)
	local nextres = resolution(attr) >= 1 and "Fine" or "Coarse"
	Cmd(string.format('Set Root "ShowData"."UserProfiles"."%s"."UserAttributePreferences"."%s" Property "EncoderResolution" "%s"',
		profile, attr, nextres))
end

local function watched(no)
	for _, n in ipairs(WATCH) do
		if n == no then return true end
	end
	return false
end

local function report(force)
	if force then last = {} end
	local page = CurrentExecPage().index
	if changed("page", page) then
		send("page", "i", page)
		last = { page = page } -- new page: resend everything below
	end
	local seen = {}
	for _, ex in ipairs(DataPool().Pages[page]:Children()) do
		local no = ex.No
		if watched(no) then
			seen[no] = true
			local ok, fader = pcall(function() return ex:GetFader({ token = "FaderMaster", faderDisabled = false }) end)
			fader = ok and fader or 0
			local obj = ex.Object
			local running = obj and obj:HasActivePlayback() and 1 or 0
			local name = obj and obj.name or ""
			if changed("f" .. no, math.floor(fader * 10 + 0.5)) then send("fader/" .. no, "f", fader) end
			if changed("r" .. no, running) then send("run/" .. no, "i", running) end
			if changed("n" .. no, name) then send("name/" .. no, "s", name) end
		end
	end
	for _, no in ipairs(WATCH) do -- executors that became empty
		if not seen[no] and last["n" .. no] and last["n" .. no] ~= "" and changed("n" .. no, "") then
			send("fader/" .. no, "f", 0)
			send("run/" .. no, "i", 0)
			send("name/" .. no, "s", "")
		end
	end
	for _, m in ipairs(MASTERS) do
		local v = master_on(m)
		if changed("m" .. m, v) then send("master/" .. m, "i", v) end
	end
	local t = cmdtext()
	if changed("cmd", t) then send("cmdline", "s", t) end
	for _, a in ipairs(ATTRS) do
		local r = resolution(a)
		if changed("res" .. a, r) then send("res/" .. a, "f", r) end
	end
end

-- ---- acting on the command line --------------------------------------

local function key(name)
	Keyboard(1, "press", name)
	Keyboard(1, "release", name)
end

local function typetext(s)
	for c in s:gmatch(".") do
		Keyboard(1, "char", c)
	end
end

local function replace_line(s) -- clear the command line, then type s
	if #cmdtext() > 0 then key("Escape") end
	typetext(s)
end

local function act(arg)
	local verb, rest = arg:match("^(%S+)%s*(.*)$")
	verb = (verb or ""):lower()
	if verb == "page" then
		local n = num(rest)
		if n and n >= 1 then Cmd("Page " .. math.floor(n)) end
		return
	end
	if verb == "res" then
		toggle_resolution(rest)
		local r = resolution(rest)
		last["res" .. rest] = r
		send("res/" .. rest, "f", r)
		return
	end
	-- Typing goes to whatever has keyboard focus: don't type into popups.
	if GetTopModal and GetTopModal() then
		send("busy", "s", "close the popup on MA's screen first")
		return
	end
	local t = cmdtext()
	if verb == "type" then
		local needs_space = #t > 0 and not t:match("%s$") and not rest:match("^[%d%.]")
		typetext((needs_space and " " or "") .. rest)
		if not rest:match("^[%d%.]+$") then typetext(" ") end -- keywords end with a space
	elseif verb == "please" then
		if #t:gsub("%s", "") > 0 then
			key("Escape")
			Cmd(t)
		end
	elseif verb == "clear" then
		if #t > 0 then key("Escape") else Cmd("Clear") end
	elseif verb == "back" then
		local shorter = t:gsub("%s*%S+%s*$", "")
		replace_line(#shorter > 0 and (shorter .. " ") or "")
	end
	-- report the new command line now rather than on the next tick
	local now = cmdtext()
	if now ~= t then
		last["cmd"] = now
		send("cmdline", "s", now)
	end
end

-- ---- entry point -------------------------------------------------------

local beat = 0
local function tick()
	report(false)
	beat = beat + TICK
	if beat >= 2 then
		beat = 0
		send("alive", "i", 1)
	end
end

-- Bridge install: no plugin object, so run from MA's Timer. The Timer API
-- documents a whole-second delay; try our tick first, fall back to 1 s.
local function start_timer(line, tick_s, attrs)
	OSC_LINE = num(line) or OSC_LINE
	TICK = num(tick_s) or TICK
	if attrs and attrs ~= "" then
		ATTRS = {}
		for a in attrs:gmatch("[^,]+") do ATTRS[#ATTRS + 1] = a end
	end
	local gen = (num(GetVar(GlobalVars(), GENVAR)) or 0) + 1
	SetVar(GlobalVars(), GENVAR, gen)
	local function step()
		if num(GetVar(GlobalVars(), GENVAR)) == gen then
			tick()
		end
	end
	report(true)
	send("alive", "i", 1)
	if not pcall(Timer, step, TICK, 1000000000) then
		TICK = 1
		Timer(step, 1, 1000000000)
	end
	send("started", "f", TICK)
end

local function main(display, arg)
	if arg and arg ~= "" then
		local line, tick_s, attrs = arg:match("^__start%s+(%S+)%s*(%S*)%s*(%S*)")
		if line then
			start_timer(line, tick_s, attrs)
		else
			act(arg)
		end
		return
	end
	if GetVar(GlobalVars(), RUNVAR) then -- already running: tap again stops it
		SetVar(GlobalVars(), RUNVAR, false)
		Printf("littlelx: stopped")
		return
	end
	SetVar(GlobalVars(), RUNVAR, true)
	SetVar(GlobalVars(), GENVAR, (num(GetVar(GlobalVars(), GENVAR)) or 0) + 1) -- stop timers
	Printf("littlelx: reporting to OSC line " .. OSC_LINE)
	report(true)
	while GetVar(GlobalVars(), RUNVAR) do
		tick()
		coroutine.yield(TICK)
	end
end

return main
