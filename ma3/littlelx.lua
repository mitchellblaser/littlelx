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
--   "probe"        print what this MA version's Lua offers (encoder diagnostics)
--   "tab Gobo2Pos" encoders show that feature of the group (MA's tabs)
--   "sets Gobo1"   report the selected fixture's named values for an attribute
--   "setv Gobo1 3" apply the 3rd of them to the selection
--   "__start <osc line> <tick> [Attr,Attr] [version]"   start reporting from a
--                  Timer (bridge install); also report those attributes'
--                  resolution, and the version so the bridge can update us
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
local VERSION = ""                  -- set by the bridge at start: it reinstalls when this differs

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

-- The sequence's colour (its Appearance) as "rrggbb", "" if it has none.
-- MA versions differ in how it is stored, so try the known forms.
local function hex3(r, g, b)
	r, g, b = num(r), num(g), num(b)
	if not (r and g and b) then return nil end
	if r <= 1 and g <= 1 and b <= 1 then r, g, b = r * 255, g * 255, b * 255 end
	local function c(v) return math.max(0, math.min(255, math.floor(v + 0.5))) end
	return string.format("%02x%02x%02x", c(r), c(g), c(b))
end
local function rgb_string(v) -- "r,g,b[,a]" or "#rrggbb"
	if type(v) ~= "string" then return nil end
	local h = v:match("^#?(%x%x%x%x%x%x)$")
	if h then return h:lower() end
	local r, g, b = v:match("([%d%.]+)%s*[,; ]%s*([%d%.]+)%s*[,; ]%s*([%d%.]+)")
	return r and hex3(r, g, b)
end
local function seq_color(obj)
	if not obj then return "" end
	local ok, c = pcall(function()
		local ap = obj.Appearance
		if ap and type(ap) ~= "string" then
			local alpha = num(ap.BackAlpha)
			if alpha == nil or alpha > 0 then
				local h = hex3(ap.BackR, ap.BackG, ap.BackB)
				if h then return h end
			end
			local h = rgb_string(ap.Color) or rgb_string(ap.BackColor)
			if h then return h end
		end
		return rgb_string(obj.Color)
	end)
	return ok and c or ""
end

local function watched(no)
	for _, n in ipairs(WATCH) do
		if n == no then return true end
	end
	return false
end

-- ---- MA's encoders ------------------------------------------------------
-- What MA's encoder bar shows: the selected feature's attributes that the
-- (first) selected fixture has, with that fixture's values. Every call is
-- guarded: MA versions differ, and a missing function just means less info.
local MAXENC = 12

local function try(f, ...)
	if type(f) ~= "function" then return nil end
	local ok, a, b = pcall(f, ...)
	if ok then return a, b end
	return nil
end

local function prop(h, ...) -- first property of h that exists, by any of these names
	for _, k in ipairs({ ... }) do
		local ok, v = pcall(function() return h[k] end)
		if ok and v ~= nil then return v end
	end
	return nil
end

local function oname(h)
	if type(h) == "string" then return h end
	if h == nil then return nil end
	local n = prop(h, "name", "Name")
	return n and tostring(n) or nil
end

local function feature_group(feat) -- walk up to the FeatureGroup
	local h = feat
	for _ = 1, 4 do
		local ok, p = pcall(function() return h:Parent() end)
		if not ok or not p then return nil end
		local okc, cls = pcall(function() return p:GetClass() end)
		if okc and cls == "FeatureGroup" then return oname(p) end
		h = p
	end
	return nil
end

-- The show's attribute definitions, read once: each feature's attributes,
-- and the features ("tabs") of each feature group in order.
local defs
local function attribute_defs()
	if defs then return defs end
	defs = { attrs_of = {}, group_of = {}, groups = {} }
	local ok, attrs = pcall(function() return ShowData().LivePatch.AttributeDefinitions.Attributes:Children() end)
	for _, a in ipairs(ok and attrs or {}) do
		local fh = prop(a, "Feature", "feature")
		local fname = oname(fh)
		if fname then
			if not defs.attrs_of[fname] then
				defs.attrs_of[fname] = {}
				local g = (type(fh) ~= "string" and feature_group(fh)) or ""
				defs.group_of[fname] = g
				defs.groups[g] = defs.groups[g] or {}
				table.insert(defs.groups[g], fname)
			end
			table.insert(defs.attrs_of[fname], { name = oname(a), pretty = tostring(prop(a, "Pretty", "pretty") or oname(a)) })
		end
	end
	return defs
end

local function feature_attrs(fname)
	return attribute_defs().attrs_of[fname] or {}
end

local function fmt_value(v)
	v = num(v)
	if not v then return nil end
	local s = string.format("%.1f", v)
	return (s:gsub("%.0$", ""))
end

-- -> value text, "p" if it is in the programmer (MA shows those in red)
local function attr_value(sf, aname)
	local ai = try(GetAttributeIndex, aname)
	if ai == nil or sf == nil then return "", "" end
	local ui = try(GetUIChannelIndex, sf, ai)
	if ui == nil then return nil end -- this fixture doesn't have it
	local p = try(GetProgPhaser, ui, false)
	if type(p) == "table" and type(p[1]) == "table" then
		local v = fmt_value(prop(p[1], "absolute", "abs", "value"))
		if v then return v, "p" end
	end
	-- not in the programmer: the value the fixture has now (MA shows it grey).
	-- Where MA keeps it differs by version: try the known places.
	local ch = try(GetUIChannel, ui)
	if type(ch) == "table" then
		local rt = num(ch.rt_index or ch.rt_channel or ch.rtchannel)
		local r = rt and try(GetRTChannel, rt)
		for _, t in ipairs({ type(r) == "table" and r or {}, ch }) do
			for _, k in ipairs({ "final_value", "finalvalue", "value", "absolute", "current", "output",
				"normed_value", "normed" }) do
				local v = fmt_value(t[k])
				if v then return v, "" end
			end
		end
	end
	return "", ""
end

-- ---- named values of an attribute (gobos, colour slots...) -------------
-- Read from the selected fixture's own fixture type, so they are right for
-- whatever is selected: its channel function for the attribute, whose
-- children are the channel sets.
local function kids(h)
	local ok, c = pcall(function() return h:Children() end)
	return ok and type(c) == "table" and c or {}
end

local function same(a, b) return a and b and tostring(a):lower() == tostring(b):lower() end

local function channel_function(sf, aname)
	local ai = try(GetAttributeIndex, aname)
	local ui = ai and try(GetUIChannelIndex, sf, ai)
	if ui == nil then return nil end
	local cf = try(GetChannelFunction, ui, ai)
	if cf ~= nil and type(cf) ~= "number" and type(cf) ~= "string" then return cf end
	local ch = try(GetUIChannel, ui) -- else: the logical channel's function for this attribute
	local lc = type(ch) == "table" and (ch.logical_channel or ch.LogicalChannel) or nil
	for _, f in ipairs(lc and type(lc) ~= "number" and kids(lc) or {}) do
		if same(oname(prop(f, "Attribute", "attribute")), aname) then return f end
	end
	return nil
end

-- A DMX value as a fraction of full. MA gives "128/1" (value / bytes) or a number.
local function dmx(v)
	if type(v) == "number" then return v <= 255 and v / 255 or v / 65535 end
	if type(v) ~= "string" then return nil end
	local a, r = v:match("^%s*(%d+)%s*/%s*(%d+)")
	if a then return tonumber(a) / (256 ^ tonumber(r) - 1) end
	local n = tonumber(v)
	if n then return n <= 255 and n / 255 or n / 65535 end
	return nil
end

-- The channel function's DMX range: its start, and the next function's start
-- on the same channel (or full) as its end.
local function function_range(cf, last_set)
	-- last_set: where its last named value starts; the range can't end before
	-- that (guards against a neighbour given in another resolution)
	local from = dmx(prop(cf, "DMXFrom", "From")) or 0
	local floor = math.max(from, last_set or from)
	local to = dmx(prop(cf, "DMXTo", "To"))
	if not to or to <= floor then
		to = 1
		local ok, parent = pcall(function() return cf:Parent() end)
		for _, f in ipairs(ok and parent and kids(parent) or {}) do
			local x = dmx(prop(f, "DMXFrom", "From"))
			if x and x > floor + 1e-9 and x - 1 / 255 < to then to = x - 1 / 255 end
		end
	end
	return from, math.max(to, floor, from + 1e-9)
end

-- Named values with the percent MA's "At" takes: where each one sits in its
-- channel function's DMX range (MA's physical units vary: Dimmer is 0..1).
local function channel_sets(sf, aname)
	local out = {}
	local cf = sf ~= nil and channel_function(sf, aname)
	if not cf then return out end
	local raw = {}
	for _, set in ipairs(kids(cf)) do
		local nm = oname(set)
		if nm and nm ~= "" then
			raw[#raw + 1] = { name = nm, f = dmx(prop(set, "DMXFrom", "From")), t = dmx(prop(set, "DMXTo", "To")),
				pf = num(prop(set, "PhysicalFrom")), pt = num(prop(set, "PhysicalTo")) }
		end
	end
	local last_set
	for _, r in ipairs(raw) do
		if r.f and (not last_set or r.f > last_set) then last_set = r.f end
	end
	local cfrom, cto = function_range(cf, last_set)
	local cpf, cpt = num(prop(cf, "PhysicalFrom")), num(prop(cf, "PhysicalTo"))
	local function pct(x, a, b) return math.max(0, math.min(100, (x - a) / (b - a) * 100)) end
	for k, r in ipairs(raw) do
		local lo, hi
		if r.f then
			local t = r.t or (raw[k + 1] and raw[k + 1].f and raw[k + 1].f - 1 / 255) or cto
			lo, hi = pct(r.f, cfrom, cto), pct(math.max(t, r.f), cfrom, cto)
		elseif r.pf and cpf and cpt and cpt ~= cpf then -- no DMX: place it by physical value
			lo, hi = pct(r.pf, cpf, cpt), pct(r.pt or r.pf, cpf, cpt)
			if lo > hi then lo, hi = hi, lo end
		end
		if lo then
			out[#out + 1] = { name = r.name, value = (lo + hi) / 2, from = lo, to = hi }
		end
	end
	return out
end

-- A value inside one of the fixture's named ranges also shows its name
-- ("0 Closed", "100 Open"), like MA's encoder bar.
local sets_cache = {}
local function value_words(sf, aname, v)
	local x = type(v) == "string" and tonumber(v) or nil
	if not x then return v end
	local key = tostring(sf) .. "/" .. aname
	local sets = sets_cache[key]
	if not sets then
		sets = channel_sets(sf, aname)
		sets_cache[key] = sets
	end
	for _, set in ipairs(sets) do
		-- like MA: a name only for a real slot; inside an unnamed ("No Feature")
		-- or wide range (a dimmer's 1..254) the number says more
		if x >= set.from - 0.25 and x <= set.to + 0.25 then
			if set.name:lower() == "no feature" or set.to - set.from > 50 then return v end
			return v .. " " .. set.name -- MA: "0 Closed", "100 Open"
		end
	end
	return v
end

local function report_sets(aname)
	local names = {}
	for n, set in ipairs(channel_sets(try(SelectionFirst), aname)) do
		if n > 48 then break end
		names[#names + 1] = (set.name:gsub("|", "/"))
	end
	send("sets", "s", aname .. "|" .. table.concat(names, "|"))
end

local function apply_set(aname, n)
	local set = channel_sets(try(SelectionFirst), aname)[num(n) or 0]
	if not set then
		send("busy", "s", "that value isn't on the selected fixture")
		return
	end
	local v = string.format("%.2f", set.value):gsub("0+$", ""):gsub("%.$", "")
	Cmd(string.format('Attribute "%s" At %s', aname, v))
	Printf(string.format("littlelx: %s %s -> At %s", aname, set.name, v))
end

local tab_choice        -- a tab picked on the controller (until MA's own selection changes)
local ma_feature        -- the feature MA had selected last time
local has_cache = {}    -- "fixture/feature" -> does the fixture have any of its attributes

local function fixture_has(sf, fname)
	local key = tostring(sf) .. "/" .. fname
	if has_cache[key] == nil then
		has_cache[key] = false
		for _, a in ipairs(feature_attrs(fname)) do
			local ai = try(GetAttributeIndex, a.name)
			if ai and try(GetUIChannelIndex, sf, ai) ~= nil then
				has_cache[key] = true
				break
			end
		end
	end
	return has_cache[key]
end

local function report_encoders()
	local feat = try(SelectedFeature)
	local mname = oname(feat) or ""
	if mname ~= ma_feature then -- MA's own tab changed: follow it
		ma_feature, tab_choice = mname, nil
	end
	local group = (feat and feature_group(feat)) or attribute_defs().group_of[mname] or ""
	local sf = try(SelectionFirst)
	-- the group's features this fixture has: the tabs (like MA's encoder bar)
	local tabs = {}
	if sf ~= nil then
		for _, f in ipairs(attribute_defs().groups[group] or { mname }) do
			if fixture_has(sf, f) then tabs[#tabs + 1] = f end
		end
	end
	local fname = mname
	for _, t in ipairs(tabs) do
		if t == tab_choice then fname = t end
	end
	local list = {}
	if fname ~= "" and sf ~= nil then -- nothing selected: the encoders have nothing to turn
		for _, a in ipairs(feature_attrs(fname)) do
			if #list >= MAXENC then break end
			local v, st = attr_value(sf, a.name)
			if v ~= nil then -- nil: the selected fixture doesn't have this attribute
				v = tostring(value_words(sf, a.name, v)):gsub("|", "/")
				list[#list + 1] = table.concat({ a.name, a.pretty, v, st, tostring(resolution(a.name)) }, "|")
			end
		end
	end
	local head = fname .. "|" .. group .. "|" .. (sf ~= nil and "1" or "0")
	if changed("feat", head) then
		send("feat", "s", head)
		sets_cache, has_cache = {}, {} -- other feature / selection: look again
	end
	local t = table.concat(tabs, "|")
	if changed("tabs", t) then send("tabs", "s", t) end
	if changed("encn", #list) then send("encn", "i", #list) end
	for k, e in ipairs(list) do
		if changed("enc" .. k, e) then send("enc/" .. k, "s", e) end
	end
	for k = #list + 1, MAXENC do last["enc" .. k] = nil end
end

-- "probe": print what this MA version offers, to the command line feedback
local function probe()
	local out = {}
	local function say(...) local t = {} for i, v in ipairs({ ... }) do t[i] = tostring(v) end out[#out + 1] = table.concat(t, " ") end
	local function dump(name, t)
		if type(t) ~= "table" then say(name, "=", t) return end
		local keys = {}
		for k, v in pairs(t) do keys[#keys + 1] = tostring(k) .. "=" .. (type(v) == "table" and "{..}" or tostring(v)) end
		table.sort(keys)
		say(name, "{", table.concat(keys, ", "), "}")
	end
	for _, f in ipairs({ "SelectedFeature", "SelectionFirst", "GetAttributeIndex", "GetUIChannelIndex",
		"GetProgPhaser", "GetUIChannel", "GetRTChannel", "CurrentProfile", "ShowData" }) do
		say(f, type(_G and _G[f]))
	end
	local feat = try(SelectedFeature)
	say("feature:", oname(feat), "group:", feat and feature_group(feat))
	if feat then
		local ok, cls = pcall(function() return feat:GetClass() end)
		say("feature class:", ok and cls)
		local list = feature_attrs(oname(feat) or "")
		local names = {}
		for _, a in ipairs(list) do names[#names + 1] = a.name .. "(" .. a.pretty .. ")" end
		say("attributes:", table.concat(names, " "))
	end
	local okA, attrs = pcall(function() return ShowData().LivePatch.AttributeDefinitions.Attributes:Children() end)
	if okA and attrs and attrs[1] then
		say("attribute #1:", oname(attrs[1]), "Feature=", tostring(prop(attrs[1], "Feature")), oname(prop(attrs[1], "Feature")))
	else
		say("attribute definitions:", okA, tostring(attrs))
	end
	local okP, execs = pcall(function() return DataPool().Pages[CurrentExecPage().index]:Children() end)
	for _, ex in ipairs(okP and execs or {}) do
		local obj = prop(ex, "Object")
		if obj then
			say("executor", ex.No, "object:", oname(obj), "colour we read:", seq_color(obj))
			local ap = prop(obj, "Appearance", "appearance")
			say("  Appearance:", type(ap), tostring(ap), oname(ap))
			if ap and type(ap) ~= "string" then
				for _, k in ipairs({ "BackR", "BackG", "BackB", "BackAlpha", "Color", "BackColor", "ImageR", "ImageG", "ImageB" }) do
					say("  Appearance." .. k, "=", tostring(prop(ap, k)))
				end
			end
			say("  object Color =", tostring(prop(obj, "Color", "color")))
			break
		end
	end
	local sf = try(SelectionFirst)
	say("selection first:", sf)
	-- named values (gobos etc.) of the selected feature's attributes
	local function cls(h)
		local ok, c = pcall(function() return h:GetClass() end)
		return ok and c or type(h)
	end
	local function show_sets(label, h)
		say("  " .. label .. ":", cls(h), oname(h))
		for n, f in ipairs(kids(h)) do
			if n > 6 then say("    ...") break end
			say("    " .. cls(f), oname(f), "From=", tostring(prop(f, "DMXFrom", "From")),
				"PhysFrom=", tostring(prop(f, "PhysicalFrom")), "PhysTo=", tostring(prop(f, "PhysicalTo")))
			for m, set in ipairs(kids(f)) do
				if m > 8 then say("      ...") break end
				say("      " .. cls(set), oname(set), "From=", tostring(prop(set, "DMXFrom", "From")),
					"PhysFrom=", tostring(prop(set, "PhysicalFrom")), "PhysTo=", tostring(prop(set, "PhysicalTo")))
			end
		end
	end
	if sf ~= nil and feat then
		for n, a in ipairs(feature_attrs(oname(feat) or "")) do
			if n > 2 then break end
			local ai = try(GetAttributeIndex, a.name)
			local ui = ai and try(GetUIChannelIndex, sf, ai)
			say("sets for", a.name, "attr", ai, "ui", ui)
			if ui then
				local pp = try(GetProgPhaser, ui, false)
				dump("  programmer " .. a.name, type(pp) == "table" and pp[1] or pp)
			end
			local cf = channel_function(sf, a.name)
			say("  channel function found:", cf ~= nil and (cls(cf) .. " " .. tostring(oname(cf))) or "no")
			if cf then
				say("  function DMXFrom=", tostring(prop(cf, "DMXFrom", "From")), "DMXTo=", tostring(prop(cf, "DMXTo", "To")),
					"PhysicalFrom=", tostring(prop(cf, "PhysicalFrom")), "PhysicalTo=", tostring(prop(cf, "PhysicalTo")))
				local okp, parent = pcall(function() return cf:Parent() end)
				for n, f in ipairs(okp and parent and kids(parent) or {}) do
					if n > 8 then break end
					say("  neighbour", cls(f), oname(f), "DMXFrom=", tostring(prop(f, "DMXFrom", "From")))
				end
			end
			local found = {}
			for _, set in ipairs(channel_sets(sf, a.name)) do found[#found + 1] = set.name .. "=" .. set.value end
			say("  named values:", #found > 0 and table.concat(found, ", ") or "none")
			if ui then
				local ch = try(GetUIChannel, ui)
				dump("  GetUIChannel", ch)
				for _, k in ipairs({ "logical_channel", "LogicalChannel", "channel_function", "dmx_channel" }) do
					local h = type(ch) == "table" and ch[k]
					if h and type(h) ~= "number" then show_sets(k, h) end
				end
				for nargs = 2, 1, -1 do
					local cf = nargs == 2 and try(GetChannelFunction, ui, ai) or try(GetChannelFunction, ui)
					if cf ~= nil then
						if type(cf) == "table" or type(cf) == "userdata" then show_sets("GetChannelFunction/" .. nargs, cf)
						else say("  GetChannelFunction/" .. nargs, "=", tostring(cf)) end
					end
				end
			end
		end
	end
	if sf ~= nil then
		local ai = try(GetAttributeIndex, "DIMMER") or try(GetAttributeIndex, "Dimmer")
		local ui = ai and try(GetUIChannelIndex, sf, ai)
		say("dimmer attr index", ai, "ui channel", ui)
		if ui then
			local p = try(GetProgPhaser, ui, false)
			dump("GetProgPhaser", p)
			if type(p) == "table" then dump("GetProgPhaser[1]", p[1]) end
			local ch = try(GetUIChannel, ui)
			dump("GetUIChannel", ch)
			local rt = type(ch) == "table" and num(ch.rt_index or ch.rt_channel or ch.rtchannel)
			if rt then dump("GetRTChannel", try(GetRTChannel, rt)) else say("no rt channel index in GetUIChannel") end
		end
	end
	-- one OSC message per line (one big one is too long to arrive), then an
	-- end marker: the bridge saves them to a file
	for _, l in ipairs(out) do
		Printf("littlelx probe: " .. l)
		send("probe", "s", l:sub(1, 400))
	end
	send("probe_end", "i", #out)
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
			local color = seq_color(obj)
			if changed("c" .. no, color) then send("color/" .. no, "s", color) end
		end
	end
	for _, no in ipairs(WATCH) do -- executors that became empty
		if not seen[no] and last["n" .. no] and last["n" .. no] ~= "" and changed("n" .. no, "") then
			send("fader/" .. no, "f", 0)
			send("run/" .. no, "i", 0)
			send("name/" .. no, "s", "")
			send("color/" .. no, "s", "")
			last["c" .. no] = ""
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
	report_encoders()
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
	if verb == "probe" then
		probe()
		return
	end
	if verb == "tab" then -- "tab Gobo2Pos": the encoders show that feature
		tab_choice = rest
		last["feat"] = nil
		report_encoders()
		return
	end
	if verb == "sets" then -- "sets Gobo1": report its named values
		report_sets(rest)
		return
	end
	if verb == "setv" then -- "setv Gobo1 3": apply the 3rd of them
		local a, n = rest:match("^(%S+)%s+(%S+)")
		if a then apply_set(a, n) end
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
		send("ver", "s", VERSION)
	end
end

-- Bridge install: no plugin object, so run from MA's Timer. The Timer API
-- documents a whole-second delay; try our tick first, fall back to 1 s.
local function start_timer(line, tick_s, attrs, ver)
	OSC_LINE = num(line) or OSC_LINE
	VERSION = ver or ""
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
	send("ver", "s", VERSION)
	if not pcall(Timer, step, TICK, 1000000000) then
		TICK = 1
		Timer(step, 1, 1000000000)
	end
	send("started", "f", TICK)
end

local function main(display, arg)
	if arg and arg ~= "" then
		local line, tick_s, attrs, ver = arg:match("^__start%s+(%S+)%s*(%S*)%s*(%S*)%s*(%S*)")
		if line then
			start_timer(line, tick_s, attrs, ver)
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
