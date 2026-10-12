-- george_huey_export.lua : UH-1H Huey crew link for the George AI middleware.
--
-- Only active while you fly the UH-1H. Does two things:
--  1. READS (5x per second) and sends to UDP 127.0.0.1:5008:
--       engine / rotor / fuel gauge needle positions and caution lamps (cockpit draw arguments,
--       converted to RPM, degrees, PSI and pounds by the middleware), flares and chaff left,
--       and the text of the gunners' Crew Status panel (HEALTH / ROE / AMMO / BURST) when DCS
--       lists it.
--  2. ACCEPTS a short, fixed list of crew commands on UDP 127.0.0.1:5009 and nothing else:
--       weapon system (device 9) commands 3026, 3028-3030 (game autopilot on/off and modes) and
--       3037-3040 (Crew Status panel, gunner ROE and burst). These are the same commands the
--       game's own key bindings send. Anything else is ignored and noted in the status file.
--
-- Loaded from Export.lua with:   dofile(lfs.writedir()..[[Scripts\george_huey_export.lua]])
-- Status file: Saved Games\DCS\Logs\george_huey_export_status.txt

local gh_prev_next = LuaExportActivityNextEvent
local gh_out, gh_in = nil, nil
local gh_noted = {}
local gh_next_poll, gh_next_check, gh_next_scan = 0, 0, 0
local gh_active = false
local gh_panel_id = nil
local GH_PERIOD = 0.2
local GH_MARKER = "CREW STATUS"
local gh_status_path = lfs.writedir() .. [[Logs\george_huey_export_status.txt]]

-- Cockpit draw arguments sent every cycle (see Cockpit\Scripts\mainpanel_init.lua, lamps.lua)
local GH_ARGS = { 113, 114, 115, 116, 119, 120, 121, 122, 123, 124, 126, 149, 150, 239,
                  91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107,
                  108, 109, 110, 254, 255, 275, 276, 277 }

-- device id -> allowed command ids
local GH_ALLOWED = { [9] = { [3026] = true, [3028] = true, [3029] = true, [3030] = true,
                             [3037] = true, [3038] = true, [3039] = true, [3040] = true } }

local function gh_note(key, msg)
    if gh_noted[key] then return end
    gh_noted[key] = true
    local f = io.open(gh_status_path, key == "loaded" and "w" or "a")
    if f then
        f:write(os.date("%H:%M:%S") .. "  " .. msg .. "\n")
        f:close()
    end
end

gh_note("loaded", "george_huey_export.lua loaded")

local function gh_socket()
    local sock = rawget(_G, "socket")
    if type(sock) == "table" and sock.udp then return sock end
    local ok, res = pcall(require, "socket")
    if not ok then
        gh_note("require", "FAILED to load LuaSocket: " .. tostring(res))
        return nil
    end
    return res
end

local function gh_open()
    if gh_out and gh_in then return true end
    local sock = gh_socket()
    if not sock then return false end
    if not gh_out then
        local u = sock.udp()
        u:settimeout(0)
        local ok, err = u:setpeername("127.0.0.1", 5008)
        if not ok then
            gh_note("peer", "setpeername failed: " .. tostring(err))
            return false
        end
        gh_out = u
    end
    if not gh_in then
        local u = sock.udp()
        u:settimeout(0)
        local ok, err = u:setsockname("127.0.0.1", 5009)
        if not ok then
            gh_note("bind", "cannot listen on 5009 (commands disabled): " .. tostring(err))
        else
            gh_in = u
        end
    end
    gh_note("open", "UDP ready: data -> 127.0.0.1:5008, commands <- 127.0.0.1:5009")
    return gh_out ~= nil
end

local function gh_is_huey()
    if not LoGetSelfData then return false end
    local ok, me = pcall(LoGetSelfData)
    return ok and type(me) == "table" and me.Name == "UH-1H"
end

local function gh_read(id)
    local ok, txt = pcall(list_indication, id)
    if ok and type(txt) == "string" then return txt end
    return ""
end

local function gh_panel_text(t)
    if gh_panel_id then
        local txt = gh_read(gh_panel_id)
        if txt:find(GH_MARKER, 1, true) then return txt end
    end
    if t < gh_next_scan then return nil end
    gh_next_scan = t + 2.0
    for id = 0, 60 do
        local txt = gh_read(id)
        if txt:find(GH_MARKER, 1, true) then
            gh_panel_id = id
            gh_note("panel" .. id, "Crew Status panel found on indicator " .. id)
            return txt
        end
    end
    return nil
end

local function gh_commands()
    if not gh_in then return end
    for _ = 1, 8 do
        local msg = gh_in:receive()
        if not msg then return end
        local dev, cmd, val = msg:match("^C%s+(%d+)%s+(%d+)%s+(-?[%d%.]+)")
        dev, cmd, val = tonumber(dev), tonumber(cmd), tonumber(val)
        if dev and cmd and val and GH_ALLOWED[dev] and GH_ALLOWED[dev][cmd] then
            local d = GetDevice(dev)
            local ok, err = false, "no device"
            if d then ok, err = pcall(d.performClickableAction, d, cmd, val) end
            if not ok and d and d.SetCommand then ok, err = pcall(d.SetCommand, d, cmd, val) end
            if ok then
                gh_note("cmdok", "first crew command executed (" .. msg .. ")")
            else
                gh_note("cmderr" .. cmd, "command " .. msg .. " failed: " .. tostring(err))
            end
        else
            gh_note("reject" .. tostring(msg), "ignored command not on the allow list: " .. tostring(msg))
        end
    end
end

local function gh_poll(t)
    if t >= gh_next_check then
        gh_next_check = t + 2.0
        local was = gh_active
        gh_active = gh_is_huey()
        if gh_active and not was then gh_note("huey", "UH-1H detected, link active") end
    end
    if not gh_active then return end
    if not gh_open() then return end
    gh_commands()
    if t < gh_next_poll then return end
    gh_next_poll = t + GH_PERIOD

    local panel = GetDevice(0)
    local parts = {}
    if panel then
        for _, a in ipairs(GH_ARGS) do
            local ok, v = pcall(panel.get_argument_value, panel, a)
            if ok and type(v) == "number" then parts[#parts + 1] = string.format("%d=%.4f", a, v) end
        end
    end
    local chaff, flare = -1, -1
    if LoGetSnares then
        local ok, s = pcall(LoGetSnares)
        if ok and type(s) == "table" then chaff, flare = s.chaff or -1, s.flare or -1 end
    end
    local crew = gh_panel_text(t)
    local payload = "HUEY 1\nac=UH-1H\nargs=" .. table.concat(parts, ",")
        .. string.format("\nsnares=%d,%d\n", chaff, flare)
        .. (crew and ("---CREW---\n" .. crew) or "")
    local n, err = gh_out:send(payload)
    if n then gh_note("sent", "first packet sent") else gh_note("senderr", "send failed: " .. tostring(err)) end
end

function LuaExportActivityNextEvent(t)
    local t_next = t + 0.05
    if gh_prev_next then
        local ok, r = pcall(gh_prev_next, t)
        if ok and type(r) == "number" and r < t_next then t_next = r end
    end
    local ok, err = pcall(gh_poll, t)
    if not ok then gh_note("pollerr", "poll error: " .. tostring(err)) end
    return t_next
end
