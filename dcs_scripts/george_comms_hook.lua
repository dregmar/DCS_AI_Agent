-- george_comms_hook.lua : lets the George AI middleware read and drive the DCS radio (comms) menu.
-- Goes in  Saved Games\DCS\Scripts\Hooks\  (DCS loads every .lua file there at start-up).
--
-- It runs in DCS's user-interface Lua and finds the comms menu module (Scripts\UI\RadioCommandDialogPanel,
-- RadioCommandDialogsPanel) in whichever DCS Lua state holds it. It only:
--   * sends the menu's caption and its F1..F12 item texts to UDP 127.0.0.1:5011 while it is open,
--     and "MENU 0" while it is closed;
--   * accepts these commands on UDP 127.0.0.1:5012 and nothing else:
--       GROUP name    report this group's route / flight (the program learns your real group from
--                     Export.lua; the mission's "Player" unit can be another aircraft)
--       OPEN          open the menu (same as pressing \)
--       SELECT n      choose item n (same as pressing Fn), n = 1..12
--       BACK          previous menu (F11)
--       CLOSE         close the menu
-- It also sends your aircraft's mission route (LoGetRoute gives nothing for the AH-64D and UH-1H),
-- read from the loaded mission, every 10 s:  "ROUTE n,north,east;n,north,east;..."  (metres, DCS
-- world x / z; n = waypoint number, 0 = the start point, as in the mission editor).
-- And your flight from the same mission data, for wingman orders (tab-separated, one unit a line,
-- in flight order: 1 = lead):  "FLIGHT <group>\n<n>\t<unit name>\t<skill>\t<1 if it's you>\n..."
-- And the loaded mission's briefing, every 30 s, for George's and Overlord's briefing answers:
--   "BRIEF <mission name>\n##SITUATION\n<text>\n##TASK\n<your coalition's task>\n##SORTIE\n<sortie>"
-- JTAC lasing (3 Nov): every 10 s, your side's laser sources, one a line (tab-separated):
--   "JTACS\n<id>\t<unit name>\t<label>\t<type>\t<uav|named>\t<a|g>\t<lat>\t<lon>\t<alt m>"
--   drones by DCS's "UAVs" attribute; others by a unit or group name with the word JTAC, FAC, observer,
--   recon or SOF. And on request "LOS\t<req>\t<target id>\t<unit name>\t..." (terrain line of sight,
--   land.isVisible, from each unit's eye to the target): "LOS <req> <target id>\n<unit name>\t<1|0|gone>\t<slant m>..."
--   or "... \nNOTARGET". Read-only: nothing in the mission is changed.
-- Status file: Saved Games\DCS\Logs\george_comms_hook_status.txt

local gch = { noted = {}, next_t = 0, last = "", last_t = -10 }
local gch_status = lfs.writedir() .. [[Logs\george_comms_hook_status.txt]]

local function note(key, msg)
    if gch.noted[key] then return end
    gch.noted[key] = true
    local f = io.open(gch_status, key == "loaded" and "w" or "a")
    if f then
        f:write(os.date("%H:%M:%S") .. "  " .. msg .. "\n")
        f:close()
    end
end

note("loaded", "george_comms_hook.lua loaded")

local function open_sockets()
    if gch.out and gch.inp then return true end
    package.path = package.path .. ";.\\LuaSocket\\?.lua"
    package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"
    local ok, socket = pcall(require, "socket")
    if not ok then
        note("socket", "LuaSocket not available: " .. tostring(socket))
        return false
    end
    if not gch.out then
        gch.out = socket.udp()
        gch.out:settimeout(0)
        gch.out:setpeername("127.0.0.1", 5011)
    end
    if not gch.inp then
        local u = socket.udp()
        u:settimeout(0)
        local okb, err = u:setsockname("127.0.0.1", 5012)
        if okb then gch.inp = u else note("bind", "cannot listen on 5012: " .. tostring(err)) end
    end
    return gch.out ~= nil
end

-- The menu module can live in this (GUI) Lua state or in another one. Find it once, then use it
-- directly, or through net.dostring_in(state, code) when it is elsewhere.
local FIND = [[return tostring((package and package.loaded and type(package.loaded["RadioCommandDialogsPanel"]) == "table")
    or type(rawget(_G, "RadioCommandDialogsPanel")) == "table")]]
local READ = [[
local p = (package and package.loaded and package.loaded["RadioCommandDialogsPanel"]) or rawget(_G, "RadioCommandDialogsPanel")
if type(p) ~= "table" then return "NOPANEL" end
local okv, vis = pcall(p.isVisible)
if not okv or not vis then return "MENU 0\n" end
local out = { "MENU 1" }
local ok, err = pcall(function()
    local d = p.curDialogIt and p.curDialogIt.element_
    out[#out + 1] = "CAPTION " .. ((d and d.menuCaption and d.menuCaption:getText()) or "")
    for k = 1, 12 do
        local it = d and d.commandMenu and d.commandMenu:getItem(k)
        local t = it and it:getText() or ""
        if t ~= "" then out[#out + 1] = "ITEM " .. k .. " " .. t end
    end
end)
if not ok then return "MENU 1\nERROR " .. tostring(err) .. "\n" end
return table.concat(out, "\n") .. "\n"
]]
local CALL = [[
local p = (package and package.loaded and package.loaded["RadioCommandDialogsPanel"]) or rawget(_G, "RadioCommandDialogsPanel")
if type(p) ~= "table" then return "NOPANEL" end
local ok, err = pcall(%s)
return ok and "OK" or ("ERR " .. tostring(err))
]]
local STATES = { "gui", "mission", "export", "config", "server", "scripting" }

local function run(state, code)
    if state == "gui" then
        local f, err = loadstring(code)
        if not f then return "ERR " .. tostring(err) end
        local ok, res = pcall(f)
        return ok and tostring(res) or ("ERR " .. tostring(res))
    end
    local ok, res = pcall(net.dostring_in, state, code)
    return ok and tostring(res) or ("ERR " .. tostring(res))
end

local function find_state(now)
    if gch.state then return gch.state end
    if now < (gch.next_find or 0) then return nil end
    gch.next_find = now + 3.0
    local seen = {}
    for _, st in ipairs(STATES) do
        local r = run(st, FIND)
        seen[#seen + 1] = st .. "=" .. r
        if r == "true" then
            gch.state = st
            note("found", "comms menu module found in the '" .. st .. "' Lua state")
            return st
        end
    end
    note("search", "comms menu not found yet, looked in: " .. table.concat(seen, ", "))
    return nil
end

local CMDS = { OPEN = "p.setShowMenu, true", CLOSE = "p.setShowMenu, false", BACK = "p.toPrevMenu" }

local function command(state, msg)
    local cmd, arg = msg:match("^(%u+)%s*(%d*)")
    local call = CMDS[cmd]
    if cmd == "SELECT" and tonumber(arg) and tonumber(arg) >= 1 and tonumber(arg) <= 12 then
        call = "p.selectMenuItem, " .. tonumber(arg)
    end
    if not call then
        note("bad" .. tostring(msg), "ignored command: " .. tostring(msg))
        return
    end
    local r = run(state, string.format(CALL, call))
    if r == "OK" then note("cmdok", "first menu command done (" .. msg .. ")")
    else note("cmderr" .. cmd, "menu command " .. msg .. " failed: " .. r) end
end

-- Your group in the loaded mission: the unit in your multiplayer slot, else the "Player" unit.
local function mission_route()
    local okm, m = pcall(DCS.getCurrentMission)
    if not okm or type(m) ~= "table" then return nil, "no mission loaded" end
    local mis = m.mission or m
    local slot = ""
    pcall(function() slot = tostring(net.get_player_info(net.get_my_player_id(), "slot") or "") end)
    local by_slot, by_player, by_name
    local side_of = {}
    for side_name, side in pairs(mis.coalition or {}) do
        for _, ctry in ipairs(side.country or {}) do
            for _, cat in ipairs({ "helicopter", "plane" }) do
                for _, g in ipairs((ctry[cat] and ctry[cat].group) or {}) do
                    for _, u in ipairs(g.units or {}) do
                        if slot ~= "" and tostring(u.unitId) == slot then by_slot = g end
                        if u.skill == "Player" and not by_player then by_player = g end
                        if gch.want_group and g.name == gch.want_group then by_name = g end
                        side_of[g] = tostring(side_name)
                    end
                end
            end
        end
    end
    local g = by_name or by_slot or by_player
    if not g then return nil, "your aircraft isn't in the mission data (slot '" .. slot .. "')" end
    gch.side = side_of[g] or gch.side
    local roster = { "FLIGHT " .. tostring(g.name) }
    for i, u in ipairs(g.units or {}) do
        local me = (by_slot and tostring(u.unitId) == slot) or (not by_slot and u.skill == "Player")
        if by_name then me = false end          -- the program knows which unit is you (Export.lua)
        roster[#roster + 1] = string.format("%d\t%s\t%s\t%d", i, tostring(u.name), tostring(u.skill), me and 1 or 0)
    end
    gch.flight_text = table.concat(roster, "\n")
    local pts, names = {}, {}
    local function wp_name(s)                    -- a waypoint name, or its dictionary entry (5 Nov: 9-line IP)
        if type(s) ~= "string" then return "" end
        if s:sub(1, 8) == "DictKey_" then
            for _, d in pairs({ m.dictionary, mis.dictionary, type(m.l10n) == "table" and type(m.l10n.DEFAULT) == "table"
                                and m.l10n.DEFAULT.dictionary or nil }) do
                if type(d) == "table" and type(d[s]) == "string" then return d[s] end
            end
            return ""
        end
        return s
    end
    for i, p in ipairs((g.route and g.route.points) or {}) do
        if type(p.x) == "number" and type(p.y) == "number" then
            pts[#pts + 1] = string.format("%d,%.0f,%.0f", i - 1, p.x, p.y)
            local nm = wp_name(p.name):gsub("[\t\r\n;]", " ")
            if nm ~= "" then names[#names + 1] = string.format("%d\t%.0f\t%.0f\t%s", i - 1, p.x, p.y, nm) end
        end
    end
    -- "WPNAMES\n<n>\t<north m>\t<east m>\t<name>...": your route's named waypoints (the 9-line's IP)
    gch.wpnames_text = "WPNAMES" .. (#names > 0 and ("\n" .. table.concat(names, "\n")) or "")
    return "ROUTE " .. table.concat(pts, ";"), string.format("group '%s' (found by %s): %d route points",
        tostring(g.name), by_name and "name" or by_slot and "slot" or "Player skill", #pts)
end

-- Mission texts can be dictionary keys ("DictKey_descriptionText_1"): look them up.
local function mission_text(m, s)
    if type(s) ~= "string" then return "" end
    if s:sub(1, 8) ~= "DictKey_" then return s end
    local mis = m.mission or {}
    local dicts = { m.dictionary, mis.dictionary,
        type(m.l10n) == "table" and type(m.l10n.DEFAULT) == "table" and m.l10n.DEFAULT.dictionary or nil }
    for _, d in pairs(dicts) do
        if type(d) == "table" and type(d[s]) == "string" then return d[s] end
    end
    -- 22 Oct: the hook's mission table has no dictionary; the mission scripting state resolves
    -- DictKeys with env.getValueDictByKey (MIST does the same)
    if s:match("^[%w_]+$") then
        for _, st in ipairs(gch.types_state and { gch.types_state } or { "server", "mission", "scripting" }) do
            local r = run(st, 'return (env and env.getValueDictByKey) and tostring(env.getValueDictByKey("' .. s ..
                '") or "") or "NOENV"')
            if r ~= "NOENV" and r:sub(1, 4) ~= "ERR " and r ~= s and r ~= "false" then
                note("dictok" .. st, "mission texts resolved with env.getValueDictByKey in the '" .. st .. "' state")
                return r
            end
        end
    end
    note("dict" .. s, "mission text " .. s .. " not found in the mission dictionary (sent empty)")
    return ""
end

local function mission_brief()
    local okm, m = pcall(DCS.getCurrentMission)
    if not okm or type(m) ~= "table" then return nil, "no mission loaded" end
    local mis = m.mission or m
    local name, desc = "", ""
    pcall(function() name = tostring(DCS.getMissionName() or "") end)
    pcall(function() desc = tostring(DCS.getMissionDescription() or "") end)
    if desc == "" or desc:sub(1, 8) == "DictKey_" then desc = mission_text(m, mis.descriptionText) end
    local side = gch.side or "blue"
    local task = mission_text(m, side == "red" and mis.descriptionRedTask or mis.descriptionBlueTask)
    local sortie = mission_text(m, mis.sortie)
    local text = "BRIEF " .. name .. "\n##SITUATION\n" .. desc .. "\n##TASK\n" .. task .. "\n##SORTIE\n" .. sortie
    return text:sub(1, 30000), string.format("'%s', %s side: situation %d chars, task %d chars",
        name, side, #desc, #task)
end

local function send_brief(now)
    if now < (gch.brief_t or 0) then return end
    gch.brief_t = now + 30.0
    local ok, text, info = pcall(mission_brief)
    if not ok then note("brieferr", "reading the mission briefing failed: " .. tostring(text)) return end
    if not text then return end
    gch.out:send(text)
    note("brief" .. tostring(info), "mission briefing sent: " .. tostring(info))
end

-- Unit types in the mission, with DCS's own attributes (for threat ranking of types the program has
-- never seen; common/unit_types.py): "TYPES\n<type>\t<display name>\t<category>\t<attr,attr,...>\n..."
-- Asked of the mission scripting state (the one with coalition.getGroups) every 60 s.
local TYPES_CODE = [==[
if type(coalition) ~= "table" or type(coalition.getGroups) ~= "function" then return "NOAPI" end
local out, seen = {}, {}
local function add(obj, cat)
    local okn, tn = pcall(function() return obj:getTypeName() end)
    if not okn or not tn or seen[tn] then return end
    seen[tn] = true
    local okd, d = pcall(function() return obj:getDesc() end)
    d = (okd and type(d) == "table") and d or {}
    local attrs = {}
    for k, v in pairs(d.attributes or {}) do if v then attrs[#attrs + 1] = tostring(k) end end
    out[#out + 1] = tn .. "\t" .. tostring(d.displayName or tn) .. "\t" .. cat .. "\t" .. table.concat(attrs, ",")
end
local cats = { [0] = "plane", [1] = "helicopter", [2] = "ground", [3] = "ship" }
for side = 0, 2 do
    for c, name in pairs(cats) do
        for _, g in pairs(coalition.getGroups(side, c) or {}) do
            local okU, units = pcall(function() return g:getUnits() end)
            for _, u in pairs(okU and units or {}) do add(u, name) end
        end
    end
    local okS, statics = pcall(coalition.getStaticObjects, side)
    for _, st in pairs(okS and statics or {}) do add(st, "static") end
end
return "TYPES\n" .. table.concat(out, "\n")
]==]

-- Airfields, FARPs and ships with their coalition, for "nearest friendly field" (common/airfields.py):
-- "AIRBASES\n<name>\t<coalition 0/1/2>\t<category 0 airdrome, 1 helipad/FARP, 2 ship>\t<lat>\t<lon>\n..."
local AIRBASES_CODE = [==[
if type(world) ~= "table" or type(world.getAirbases) ~= "function" or type(coord) ~= "table" then return "NOAPI" end
local out = {}
for _, ab in pairs(world.getAirbases() or {}) do
    local ok, line = pcall(function()
        local lat, lon = coord.LOtoLL(ab:getPoint())
        local d = ab:getDesc() or {}
        return string.format("%s\t%d\t%d\t%.6f\t%.6f", ab:getName() or ab:getCallsign() or "?",
            ab:getCoalition() or 0, d.category or 0, lat, lon)
    end)
    if ok and line then out[#out + 1] = line end
end
return "AIRBASES\n" .. table.concat(out, "\n")
]==]

local function send_types(now)
    if now < (gch.types_t or 0) then return end
    gch.types_t = now + 60.0
    local states = gch.types_state and { gch.types_state } or { "server", "mission", "scripting", "gui" }
    for _, st in ipairs(states) do
        local r = run(st, TYPES_CODE)
        if r:sub(1, 6) == "TYPES\n" then
            gch.types_state = st
            gch.out:send(r:sub(1, 60000))
            local ab = run(st, AIRBASES_CODE)
            if ab:sub(1, 9) == "AIRBASES\n" then
                gch.out:send(ab:sub(1, 60000))
                note("airbases", "airbases sent from the '" .. st .. "' Lua state")
            else
                note("airbasesno", "no airbases: " .. ab:sub(1, 80))
            end
            local n = select(2, r:gsub("\n", "\n"))
            note("types" .. st, string.format("unit types sent from the '%s' Lua state (%d types)", st, n))
            return
        end
        note("typesno" .. st, "no unit types from '" .. st .. "': " .. r:sub(1, 80))
    end
end

-- Your aircraft's fuel, asked of the mission scripting state every 5 s: Unit:getFuel() (a fraction of
-- the internal maximum; above 1 with external tanks) and Unit:getDesc().fuelMassMax (that airframe's
-- internal maximum in kg). The program multiplies them, so nothing about the aircraft or its tanks
-- is hard-coded (24 Oct: LoGetEngineInfo's units differ per module).
local FUEL_BODY = [==[
local u, src
if name ~= "" and type(Unit) == "table" and Unit.getByName then
    local ok, x = pcall(Unit.getByName, name)
    if ok and x then u, src = x, "name" end
end
if not u and type(world) == "table" and world.getPlayer then
    local ok, x = pcall(world.getPlayer)
    if ok and x then u, src = x, "player" end
end
if not u then return "NOUNIT" end
local okf, f = pcall(function() return u:getFuel() end)
if not okf or type(f) ~= "number" then return "NOFUEL" end
local okd, d = pcall(function() return u:getDesc() end)
local max = (okd and type(d) == "table" and tonumber(d.fuelMassMax)) or -1
local okn, n = pcall(function() return u:getName() end)
return string.format("FUEL %.5f %.1f %s %s", f, max, src, okn and tostring(n) or "")
]==]

local function send_fuel(now)
    if now < (gch.fuel_t or 0) then return end
    gch.fuel_t = now + 5.0
    local states = gch.types_state and { gch.types_state } or { "server", "mission", "scripting" }
    for _, st in ipairs(states) do
        local code = "local name = " .. string.format("%q", gch.want_unit or "") .. "\n" .. FUEL_BODY
        local r = run(st, code)
        if r:sub(1, 5) == "FUEL " then
            gch.out:send(r)
            note("fuel" .. st, "fuel sent from the '" .. st .. "' Lua state: " .. r)
            return
        end
        note("fuelno" .. st .. r:sub(1, 6), "no fuel from '" .. st .. "': " .. r:sub(1, 80))
    end
end

-- Tactical picture I-T4 (1 Nov): DCS's own word that a unit died. Run in the mission scripting state
-- every 0.5 s: the first run there adds one world event handler (GEORGE_EV, a global of that state, so
-- a new mission gets a new one); it queues "kind;victim id;victim name;victim type;killer id;killer
-- name;killer type" for DEAD / KILL / UNIT_LOST / CRASH (at most 200 waiting). Each run hands back the
-- queue as "EVENTS\n<lines>". Ids are the objects' id_ (Export.lua's LoGetWorldObjects keys - the first
-- flight confirms they match). Read-only: nothing in the mission is changed.
local EVENTS_CODE = [==[
local ok, res = pcall(function()
    if type(world) ~= "table" or type(world.event) ~= "table" or not world.addEventHandler then return "NOWORLD" end
    if not GEORGE_EV then
        local E = world.event
        local kinds = {}
        if E.S_EVENT_DEAD then kinds[E.S_EVENT_DEAD] = "DEAD" end
        if E.S_EVENT_KILL then kinds[E.S_EVENT_KILL] = "KILL" end
        if E.S_EVENT_UNIT_LOST then kinds[E.S_EVENT_UNIT_LOST] = "LOST" end
        if E.S_EVENT_CRASH then kinds[E.S_EVENT_CRASH] = "CRASH" end
        local function clean(v) return (tostring(v or ""):gsub("[;\r\n]", " ")) end
        local function who(o)
            if o == nil then return "", "", "" end
            local oi, i = pcall(function() return o.id_ end)
            local on, n = pcall(function() return o:getName() end)
            local ot, t = pcall(function() return o:getTypeName() end)
            return (oi and i ~= nil) and clean(i) or "", on and clean(n) or "", ot and clean(t) or ""
        end
        GEORGE_EV = { q = {} }
        GEORGE_EV.h = { onEvent = function(self, e)
            pcall(function()
                local kind = e and kinds[e.id]
                if not kind or #GEORGE_EV.q >= 200 then return end
                local victim, killer = e.initiator, nil
                if kind == "KILL" then victim, killer = e.target, e.initiator end
                local vi, vn, vt = who(victim)
                if vi == "" then return end
                local ki, kn, kt = who(killer)
                GEORGE_EV.q[#GEORGE_EV.q + 1] = table.concat({ kind, vi, vn, vt, ki, kn, kt }, ";")
            end)
        end }
        world.addEventHandler(GEORGE_EV.h)
    end
    local q = GEORGE_EV.q
    if #q == 0 then return "EVENTS0" end
    GEORGE_EV.q = {}
    return "EVENTS\n" .. table.concat(q, "\n")
end)
if not ok then return "ERR " .. tostring(res) end
return res
]==]

local function send_events(now)
    if now < (gch.events_t or 0) then return end
    gch.events_t = now + 0.5
    local st = gch.types_state
    if not st then return end                       -- the scripting state isn't found yet (send_types)
    local r = run(st, EVENTS_CODE)
    if r:sub(1, 7) == "EVENTS\n" then
        gch.out:send(r:sub(1, 60000))
        note("events", "first DCS unit events sent from the '" .. st .. "' Lua state")
    elseif r ~= "EVENTS0" then
        note("eventsno" .. r:sub(1, 12), "no DCS unit events: " .. r:sub(1, 80))
    end
end

-- Command roles P2-2 (1 Nov): your callsign as the mission gives it (Unit:getCallsign(), e.g.
-- "Enfield11"), asked of the mission scripting state every 10 s: "CALLSIGN <callsign>\t<unit name>".
-- The program says it as "Enfield 1-1"; with nothing from here it uses OPERATOR_CALLSIGN ("NOMAD").
local CALLSIGN_BODY = [==[
local u
if name ~= "" and type(Unit) == "table" and Unit.getByName then
    local ok, x = pcall(Unit.getByName, name)
    if ok and x then u = x end
end
if not u and type(world) == "table" and world.getPlayer then
    local ok, x = pcall(world.getPlayer)
    if ok and x then u = x end
end
if not u then return "NOUNIT" end
local okc, c = pcall(function() return u:getCallsign() end)
if not okc or c == nil or tostring(c) == "" then return "NOCALLSIGN" end
local okn, n = pcall(function() return u:getName() end)
local cs = (tostring(c):gsub("[\r\n\t]", " "))
local un = okn and (tostring(n):gsub("[\r\n\t]", " ")) or ""
return "CALLSIGN " .. cs .. "\t" .. un
]==]

local function send_callsign(now)
    if now < (gch.callsign_t or 0) then return end
    gch.callsign_t = now + 10.0
    local states = gch.types_state and { gch.types_state } or { "server", "mission", "scripting" }
    for _, st in ipairs(states) do
        local r = run(st, "local name = " .. string.format("%q", gch.want_unit or "") .. "\n" .. CALLSIGN_BODY)
        if r:sub(1, 9) == "CALLSIGN " then
            gch.out:send(r)
            note("callsign" .. r, "callsign sent from the '" .. st .. "' Lua state: " .. r:gsub("\t", " / "))
            return
        end
        note("callsignno" .. st .. r:sub(1, 8), "no callsign from '" .. st .. "': " .. r:sub(1, 80))
    end
end

-- Detection probe D0 (3 Nov): what DCS's own sensors report, before a detection model is designed.
-- Runs ONLY while Saved Games\DCS\Scripts\george_detection_probe.flag exists (detection_probe.py on /
-- off), every 10 s: your side's AWACS, drones and units named JTAC / observer / FAC / recon / SOF report
-- Controller:getDetectedTargets() and isTargetDetected(); terrain line of sight (land.isVisible) to the
-- same targets and to enemy ground units within 20 km. Writes Logs\george_detection_probe.txt with the
-- real time each query took. Read-only: nothing in the mission is changed, nothing is sent.
local DPROBE_FLAG = lfs.writedir() .. [[Scripts\george_detection_probe.flag]]
local DPROBE_LOG = lfs.writedir() .. [[Logs\george_detection_probe.txt]]
local DPROBE_CODE = [==[
local MAXOBS, MAXT, LOS_M, MAXLOS = 6, 80, 20000, 30
local ok, res = pcall(function()
    if type(coalition) ~= "table" or type(world) ~= "table" or type(Group) ~= "table" then return "DPROBE NOWORLD" end
    local me
    if name ~= "" and type(Unit) == "table" and Unit.getByName then
        local o, x = pcall(Unit.getByName, name)
        if o then me = x end
    end
    if not me and world.getPlayer then
        local o, x = pcall(world.getPlayer)
        if o then me = x end
    end
    if not me then return "DPROBE NOUNIT" end
    local out = {}
    local function add(t) out[#out + 1] = t end
    local function clean(v) return (tostring(v == nil and "" or v):gsub("[\t\r\n]", " ")) end
    local function d2(a, b) local dx, dz = a.x - b.x, a.z - b.z return math.sqrt(dx * dx + dz * dz) end
    local function num(v) return type(v) == "number" and string.format("%.0f", v) or "" end
    local side = me:getCoalition()
    local enemy = (side == 1) and 2 or 1
    local myname = clean(me:getName())
    local obs = {}
    for _, cat in ipairs({ Group.Category.AIRPLANE, Group.Category.HELICOPTER, Group.Category.GROUND }) do
        local okg, groups = pcall(coalition.getGroups, side, cat)
        for _, g in ipairs(okg and groups or {}) do
            local oku, units = pcall(function() return g:getUnits() end)
            for _, u in ipairs(oku and units or {}) do
                if #obs < MAXOBS then
                    local okd, d = pcall(function() return u:getDesc() end)
                    local at = (okd and type(d) == "table" and type(d.attributes) == "table") and d.attributes or {}
                    local un = clean(u:getName())
                    local low = string.lower(un .. " " .. clean(g:getName()))
                    local why = (at["AWACS"] and "awacs") or (at["UAVs"] and "uav")
                    if not why then
                        for _, w in ipairs({ "jtac", "observer", "fac", "recon", "sof" }) do
                            if low:find(w, 1, true) then why = "named" break end
                        end
                    end
                    if why and un ~= myname then obs[#obs + 1] = { u = u, why = why, name = un } end
                end
            end
        end
    end
    local foes = {}
    local okf, fg = pcall(coalition.getGroups, enemy, Group.Category.GROUND)
    for _, g in ipairs(okf and fg or {}) do
        local oku, units = pcall(function() return g:getUnits() end)
        for _, u in ipairs(oku and units or {}) do foes[#foes + 1] = u end
    end
    add(string.format("SUMMARY\tside %d\tobservers %d\tenemy ground units %d", side, #obs, #foes))
    for _, o in ipairs(obs) do
        local okp, p = pcall(function() return o.u:getPoint() end)
        local okc, ctrl = pcall(function() return o.u:getController() end)
        if okp and okc and ctrl then
            local okt, list = pcall(function() return ctrl:getDetectedTargets() end)
            local terr = okt and "" or ("\terror " .. clean(list))
            list = okt and type(list) == "table" and list or {}
            add(string.format("OBS\t%s\t%s\t%s\talt %s\tdetected %d%s", o.name, clean(o.u:getTypeName()), o.why,
                num(p.y), #list, terr))
            local seen = {}
            for i, t in ipairs(list) do
                local obj = t.object
                if obj and i <= MAXT then
                    local okid, tid = pcall(function() return obj.id_ end)
                    if okid then seen[tostring(tid)] = true end
                    local oki, det, vis, lastTime, typeKnown, distKnown, lastPos =
                        pcall(function() return ctrl:isTargetDetected(obj) end)
                    local okq, tp = pcall(function() return obj:getPoint() end)
                    local okn, tn = pcall(function() return obj:getTypeName() end)
                    local okco, tc = pcall(function() return obj:getCoalition() end)
                    local okcat, tcat = pcall(function() return obj:getCategory() end)
                    local err, los = "", ""
                    if okq and tp and oki and type(lastPos) == "table" and lastPos.x then err = num(d2(tp, lastPos)) end
                    if okq and tp and land and land.isVisible then
                        local okl, v = pcall(land.isVisible, { x = p.x, y = p.y + 2, z = p.z }, { x = tp.x, y = tp.y + 2, z = tp.z })
                        los = okl and tostring(v) or "err"
                    end
                    add(table.concat({ "DET", o.name, clean(okid and tid), clean(okn and tn), clean(okco and tc),
                        clean(okcat and tcat), "visible=" .. tostring(t.visible), "typeKnown=" .. tostring(t.type),
                        "distKnown=" .. tostring(t.distance), "lastTime=" .. (oki and num(lastTime) or ""),
                        "range=" .. ((okq and tp) and num(d2(p, tp)) or ""), "posErr=" .. err, "los=" .. los }, "\t"))
                end
            end
            local n = 0
            for _, f in ipairs(foes) do
                if n >= MAXLOS then break end
                local okq, fp = pcall(function() return f:getPoint() end)
                if okq and fp and d2(p, fp) <= LOS_M and land and land.isVisible then
                    n = n + 1
                    local okl, v = pcall(land.isVisible, { x = p.x, y = p.y + 2, z = p.z }, { x = fp.x, y = fp.y + 2, z = fp.z })
                    local okid, fid = pcall(function() return f.id_ end)
                    add(table.concat({ "LOS", o.name, clean(okid and fid), clean(f:getTypeName()), "range=" .. num(d2(p, fp)),
                        "los=" .. (okl and tostring(v) or "err"), "detected=" .. tostring(okid and seen[tostring(fid)] == true) }, "\t"))
                end
            end
        end
    end
    return "DPROBE\n" .. table.concat(out, "\n")
end)
if not ok then return "DPROBE ERR " .. tostring(res) end
return res
]==]

local function detection_probe(now)
    if now < (gch.dprobe_t or 0) then return end
    gch.dprobe_t = now + 10.0
    if lfs.attributes(DPROBE_FLAG) == nil then gch.dprobe_on = false return end
    local st = gch.types_state
    if not st then return end
    local t0 = DCS.getRealTime()
    local r = run(st, "local name = " .. string.format("%q", gch.want_unit or "") .. "\n" .. DPROBE_CODE)
    local ms = (DCS.getRealTime() - t0) * 1000.0
    local f = io.open(DPROBE_LOG, gch.dprobe_on and "a" or "w")      -- a fresh log each time the flag appears
    gch.dprobe_on = true
    if f then
        local lines = select(2, r:gsub("\n", "\n"))
        f:write(string.format("== %s  query %.1f ms  %d lines\n%s\n", os.date("%H:%M:%S"), ms, lines, r))
        f:close()
    end
    note("dprobe", "detection probe running (flag present), log: Logs\\george_detection_probe.txt")
end

-- JTAC lasing (3 Nov): laser sources and terrain line of sight, in the mission scripting state.
local JTACS_CODE = [==[
local MAXJ = 12
local ok, res = pcall(function()
    if type(coalition) ~= "table" or type(Group) ~= "table" or type(coord) ~= "table" then return "JTACS NOWORLD" end
    local me
    if name ~= "" and type(Unit) == "table" and Unit.getByName then
        local o, x = pcall(Unit.getByName, name)
        if o then me = x end
    end
    if not me and world.getPlayer then
        local o, x = pcall(world.getPlayer)
        if o then me = x end
    end
    if not me then return "JTACS NOUNIT" end
    local function clean(v) return (tostring(v == nil and "" or v):gsub("[\t\r\n]", " ")) end
    local function named(v)
        local low = string.lower(v)
        for _, w in ipairs({ "jtac", "observer", "fac", "recon", "sof" }) do
            if low:find("%f[%a]" .. w .. "%f[%A]") then return true end
        end
        return false
    end
    local side, myname, out = me:getCoalition(), clean(me:getName()), {}
    for _, cat in ipairs({ Group.Category.GROUND, Group.Category.AIRPLANE, Group.Category.HELICOPTER }) do
        local okg, groups = pcall(coalition.getGroups, side, cat)
        for _, g in ipairs(okg and groups or {}) do
            local gname = clean(g:getName())
            local oku, units = pcall(function() return g:getUnits() end)
            for _, u in ipairs(oku and units or {}) do
                if #out >= MAXJ then break end
                pcall(function()
                    local d = u:getDesc() or {}
                    local at = type(d.attributes) == "table" and d.attributes or {}
                    local un = clean(u:getName())
                    local why = (at["UAVs"] and "uav") or ((named(un) or named(gname)) and "named") or nil
                    if why and un ~= myname and u:isExist() then
                        local p = u:getPoint()
                        local lat, lon = coord.LOtoLL(p)
                        local label = (why == "named" and not named(un)) and gname or un
                        out[#out + 1] = string.format("%s\t%s\t%s\t%s\t%s\t%s\t%.6f\t%.6f\t%.0f", clean(u.id_), un, label,
                            clean(u:getTypeName()), why, cat == Group.Category.GROUND and "g" or "a", lat, lon, p.y)
                    end
                end)
            end
        end
    end
    return "JTACS" .. (#out > 0 and ("\n" .. table.concat(out, "\n")) or "")
end)
if not ok then return "JTACS ERR " .. tostring(res) end
return res
]==]

local LOS_CODE = [==[
local ok, res = pcall(function()
    if type(coalition) ~= "table" or type(land) ~= "table" or not land.isVisible then return "NOWORLD" end
    local function find()
        for _, side in ipairs({ 1, 2, 0 }) do
            for _, cat in ipairs({ Group.Category.GROUND, Group.Category.SHIP }) do
                local okg, groups = pcall(coalition.getGroups, side, cat)
                for _, g in ipairs(okg and groups or {}) do
                    local oku, units = pcall(function() return g:getUnits() end)
                    for _, u in ipairs(oku and units or {}) do
                        if tostring(u.id_) == tid then return u end
                    end
                end
            end
        end
        return nil
    end
    local function height(o)
        local okd, d = pcall(function() return o:getDesc() end)
        if okd and type(d) == "table" and type(d.box) == "table" and type(d.box.max) == "table"
           and type(d.box.max.y) == "number" then return d.box.max.y, d.category end
        return 2, okd and type(d) == "table" and d.category or nil
    end
    local tgt = find()
    if not tgt then return "NOTARGET" end
    local tp = tgt:getPoint()
    local th = height(tgt)
    local aim = { x = tp.x, y = tp.y + math.max(0.5, math.min(th * 0.5, 3)), z = tp.z }
    local out = {}
    for _, n in ipairs(names) do
        local oko, o = pcall(Unit.getByName, n)
        if oko and o and o:isExist() then
            local op = o:getPoint()
            local oh, ocat = height(o)
            local air = (ocat == 0 or ocat == 1)                  -- Unit.Category AIRPLANE / HELICOPTER
            local eye = { x = op.x, y = op.y + (air and 0 or math.max(1.5, math.min(oh, 5))), z = op.z }
            local okv, v = pcall(land.isVisible, eye, aim)
            local dx, dy, dz = aim.x - eye.x, aim.y - eye.y, aim.z - eye.z
            out[#out + 1] = string.format("%s\t%s\t%.0f", n, (okv and v) and "1" or "0", math.sqrt(dx * dx + dy * dy + dz * dz))
        else
            out[#out + 1] = n .. "\tgone\t0"
        end
    end
    return table.concat(out, "\n")
end)
if not ok then return "ERR " .. tostring(res) end
return res
]==]

local function send_jtacs(now)
    if now < (gch.jtacs_t or 0) then return end
    gch.jtacs_t = now + 10.0
    local st = gch.types_state
    if not st then return end
    local r = run(st, "local name = " .. string.format("%q", gch.want_unit or "") .. "\n" .. JTACS_CODE)
    if r:sub(1, 5) == "JTACS" and r:sub(1, 7) ~= "JTACS N" and r:sub(1, 9) ~= "JTACS ERR" then
        gch.out:send(r:sub(1, 60000))
        local n = select(2, r:gsub("\n", "\n"))
        note("jtacs" .. n, string.format("JTAC list sent: %d laser source(s)", n))
    else
        note("jtacsno" .. r:sub(1, 12), "no JTAC list: " .. r:sub(1, 120))
    end
end

local function los_query(msg)
    local parts = {}
    for f in (msg .. "\t"):gmatch("([^\t]*)\t") do parts[#parts + 1] = f end
    local req, tid = tonumber(parts[2]), parts[3]
    if not req or not tid or not tid:match("^%-?%d+$") then
        note("losbad", "ignored line-of-sight request: " .. msg:sub(1, 80))
        return
    end
    local names = {}
    for i = 4, math.min(#parts, 11) do
        if parts[i] ~= "" then names[#names + 1] = string.format("%q", parts[i]) end
    end
    local st = gch.types_state
    local r = "NOSTATE"
    if st then
        r = run(st, string.format("local tid, names = %q, { %s }\n", tid, table.concat(names, ", ")) .. LOS_CODE)
    end
    gch.out:send(string.format("LOS %d %s\n%s", req, tid, r))
    note("los", "first line-of-sight answer: " .. r:sub(1, 80):gsub("[\t\n]", " "))
end

local function send_route(now)
    if now < (gch.route_t or 0) then return end
    gch.route_t = now + 10.0
    local ok, text, info = pcall(mission_route)
    if not ok then note("routeerr", "reading the mission route failed: " .. tostring(text)) return end
    if not text then note("noroute" .. tostring(info), "no mission route: " .. tostring(info)) return end
    gch.out:send(text)
    note("route" .. info, "mission route sent: " .. info)
    if gch.wpnames_text then
        gch.out:send(gch.wpnames_text)
        note("wpn" .. gch.wpnames_text, "waypoint names sent: " .. gch.wpnames_text:gsub("\n", " | "):sub(1, 200))
    end
    if gch.flight_text then
        gch.out:send(gch.flight_text)
        note("flight" .. gch.flight_text, "flight sent: " .. gch.flight_text:gsub("\n", " | "))
    end
end

-- "PLAYERS <n> <mp|sp>" every 5 s: how many people are connected (training targets are refused in a
-- mission with other human players, audit step 7)
local function send_players(now)
    if now < (gch.players_t or 0) then return end
    gch.players_t = now + 5.0
    local n, mp = 0, false
    local okl, list = pcall(net.get_player_list)
    if okl and type(list) == "table" then n = #list end
    pcall(function() mp = DCS.isMultiplayer() and true or false end)
    gch.out:send(string.format("PLAYERS %d %s", n, mp and "mp" or "sp"))
    note("players" .. n .. tostring(mp), string.format("players: %d (%s)", n, mp and "multiplayer" or "single player"))
end

local function frame()
    local now = DCS.getRealTime()
    if now < gch.next_t then return end
    gch.next_t = now + 0.1
    note("frame", "running (first frame)")
    if not open_sockets() then return end
    if gch.loaded and gch.loaded ~= gch.loaded_sent then
        gch.out:send("LOADED " .. tostring(gch.loaded))
        gch.loaded_sent = gch.loaded
        note("loaded" .. gch.loaded, "mission loaded (" .. gch.loaded .. ")")
    end
    send_route(now)
    send_brief(now)
    send_types(now)
    send_fuel(now)
    send_players(now)
    send_events(now)
    send_callsign(now)
    detection_probe(now)
    send_jtacs(now)
    local state = find_state(now)
    if gch.inp then
        for _ = 1, 5 do
            local msg = gch.inp:receive()
            if not msg then break end
            local want = msg:match("^GROUP (.+)$")
            local unit = msg:match("^UNIT (.+)$")
            if unit then
                if unit ~= gch.want_unit then
                    gch.want_unit, gch.fuel_t, gch.callsign_t = unit, 0, 0
                    note("unit" .. unit, "the program asked for unit '" .. unit .. "'")
                end
            elseif want then
                if want ~= gch.want_group then
                    gch.want_group, gch.route_t = want, 0           -- resend route + flight now
                    note("group" .. want, "the program asked for group '" .. want .. "'")
                end
            elseif msg:sub(1, 4) == "LOS\t" then
                los_query(msg)                                  -- JTAC lasing (3 Nov): answered without the menu
            elseif state then
                command(state, msg)
            end
        end
    end
    if not state then return end
    local text = run(state, READ)
    if text == "NOPANEL" or text:sub(1, 3) == "ERR" then
        note("readerr" .. text:sub(1, 20), "reading the menu failed: " .. text)
        if text == "NOPANEL" then gch.state = nil end          -- mission ended: look again
        return
    end
    if text ~= gch.last or now - gch.last_t > 1.0 then
        gch.out:send(text)
        gch.last, gch.last_t = text, now
        note("sent", "first menu packet sent")
    end
end

DCS.setUserCallbacks({
    onMissionLoadEnd = function()            -- new mission (or the same one restarted): send at once
        gch.route_t, gch.brief_t, gch.types_t, gch.fuel_t, gch.players_t, gch.events_t = 0, 0, 0, 0, 0, 0
        gch.callsign_t, gch.jtacs_t = 0, 0
        gch.loaded = (gch.loaded or 0) + 1      -- the program forgets this mission's flights and clones
    end,
    onSimulationFrame = function()
        local ok, err = pcall(frame)
        if not ok then note("frameerr", "error: " .. tostring(err)) end
    end,
})
