-- george_export.lua (v3) : READ-ONLY. Streams MPD indication text to George (UDP 127.0.0.1:5006)
-- so George can see which weapon (GUN / MSL / RKT) and sight are selected on the WPN page.
-- It only calls list_indication() and sends UDP. It never touches the aircraft.
--
-- Packet format (matches core/indication_listener.py):  "IND <id>\n" .. <list_indication text>
-- Indicators NOT showing a WPN page are sent with an empty body.
--
-- DIAGNOSTICS: writes  Saved Games\DCS\Logs\george_export_status.txt  describing what happened
-- (loaded / socket ok or the exact error / first packet sent / WPN page seen). If that file does
-- not exist after you start a mission, DCS never loaded this script (check the Export.lua line).
--
-- NOTE: rebuilt from core/indication_listener.py after an earlier george_export.lua was
-- overwritten by mistake. If you have the original, prefer it.
--
-- Install:
--   1. Copy to  Saved Games\DCS\Scripts\george_export.lua   (DCS.openbeta if that is your profile)
--   2. In Export.lua, REMOVE any george_dump.lua line and put this at the END instead:
--          dofile(lfs.writedir()..[[Scripts\george_export.lua]])
--   3. START A NEW MISSION (Export.lua is only read at mission start).

local george_prev_next = LuaExportActivityNextEvent
local george_next_poll = 0
local george_udp = nil
local george_noted = {}
local GEORGE_PERIOD = 1.0                  -- seconds between scans (George treats data >3 s old as stale)
local GEORGE_MARKER = "SightStatusWindow_text_2"
local george_status_path = lfs.writedir() .. [[Logs\george_export_status.txt]]

-- Writes each distinct message once, so the status file stays tiny.
local function george_note(key, msg)
    if george_noted[key] then return end
    george_noted[key] = true
    local f = io.open(george_status_path, key == "loaded" and "w" or "a")
    if f then
        f:write(os.date("%H:%M:%S") .. "  " .. msg .. "\n")
        f:close()
    end
end

george_note("loaded", "george_export.lua loaded (waiting for first poll)")

local function george_open()
    if george_udp then return true end
    local sock = rawget(_G, "socket")
    if type(sock) ~= "table" or not sock.udp then
        local ok, res = pcall(require, "socket")
        if not ok then
            package.path = package.path .. ";" .. lfs.currentdir() .. "/LuaSocket/?.lua"
            package.cpath = package.cpath .. ";" .. lfs.currentdir() .. "/LuaSocket/?.dll"
            ok, res = pcall(require, "socket")
        end
        if not ok then
            george_note("require", "FAILED to load LuaSocket: " .. tostring(res))
            return false
        end
        sock = res
    end
    local u = sock.udp()
    u:settimeout(0)
    local ok, err = u:setpeername("127.0.0.1", 5006)
    if not ok then
        george_note("peer", "setpeername failed: " .. tostring(err))
        return false
    end
    george_udp = u
    george_note("open", "UDP socket ready -> 127.0.0.1:5006")
    return true
end

local function george_poll()
    if not george_open() then return end
    local nonempty, wpn = 0, 0
    for id = 0, 31 do
        local ok, txt = pcall(list_indication, id)
        if ok and type(txt) == "string" and #txt > 0 then
            nonempty = nonempty + 1
            local has = txt:find(GEORGE_MARKER, 1, true) ~= nil
            if has then wpn = wpn + 1 end
            local n, err = george_udp:send("IND " .. id .. "\n" .. (has and txt or ""))
            if n then
                george_note("firstsend", "first packet sent OK (" .. n .. " bytes, indicator " .. id .. ")")
            else
                george_note("senderr", "send failed: " .. tostring(err))
            end
        end
    end
    if nonempty == 0 then
        george_note("empty", "list_indication returned nothing yet (not in a cockpit?)")
    else
        george_note("firstpoll", string.format("first poll with data: %d indicators, %d on a WPN page", nonempty, wpn))
    end
    if wpn > 0 then george_note("wpnseen", "WPN page seen on an MPD") end
end

function LuaExportActivityNextEvent(t)
    local t_next = t + GEORGE_PERIOD
    if george_prev_next then
        local ok, r = pcall(george_prev_next, t)
        if ok and type(r) == "number" and r < t_next then t_next = r end
    end
    if t >= george_next_poll then
        george_next_poll = t + GEORGE_PERIOD
        local ok, err = pcall(george_poll)
        if not ok then george_note("pollerr", "poll error: " .. tostring(err)) end
    end
    return t_next
end
