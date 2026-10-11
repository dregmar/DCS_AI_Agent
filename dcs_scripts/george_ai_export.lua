-- george_ai_export.lua : READ-ONLY. Streams George's AI Helper Interface (wheel labels and the
-- Target / Search / Point list window) to the George AI middleware on UDP 127.0.0.1:5007.
-- It only calls list_indication() and sends UDP. It never touches the aircraft.
--
-- Found with george_probe.lua: the interface is cockpit indicator 26 and its text contains
-- "weap_contol" (sic) while the wheel is shown. When the wheel is hidden that indicator only
-- holds "#1#". If a DCS update moves it, the script searches indicators 0..60 for the marker.
--
-- Packet:  "GAI 1\n" .. <indicator text>   wheel shown
--          "GAI 0\n"                        wheel hidden / interface not found
-- Sent when the text changes (checked 5x per second) and at least twice per second.
--
-- Loaded from Export.lua with:   dofile(lfs.writedir()..[[Scripts\george_ai_export.lua]])
-- Status file: Saved Games\DCS\Logs\george_ai_export_status.txt

local gai_prev_next = LuaExportActivityNextEvent
local gai_udp = nil
local gai_id = 26                 -- probe result; re-found automatically if it moves
local gai_confirmed = false
local gai_next_poll = 0
local gai_next_scan = 0
local gai_last_sent = ""
local gai_last_send_t = -10
local gai_noted = {}
local GAI_MARKER = "weap_contol"
local GAI_PERIOD = 0.2
local gai_status_path = lfs.writedir() .. [[Logs\george_ai_export_status.txt]]

local function gai_note(key, msg)
    if gai_noted[key] then return end
    gai_noted[key] = true
    local f = io.open(gai_status_path, key == "loaded" and "w" or "a")
    if f then
        f:write(os.date("%H:%M:%S") .. "  " .. msg .. "\n")
        f:close()
    end
end

gai_note("loaded", "george_ai_export.lua loaded")

local function gai_open()
    if gai_udp then return true end
    local sock = rawget(_G, "socket")
    if type(sock) ~= "table" or not sock.udp then
        local ok, res = pcall(require, "socket")
        if not ok then
            gai_note("require", "FAILED to load LuaSocket: " .. tostring(res))
            return false
        end
        sock = res
    end
    local u = sock.udp()
    u:settimeout(0)
    local ok, err = u:setpeername("127.0.0.1", 5007)
    if not ok then
        gai_note("peer", "setpeername failed: " .. tostring(err))
        return false
    end
    gai_udp = u
    gai_note("open", "UDP socket ready -> 127.0.0.1:5007")
    return true
end

local function gai_read(id)
    local ok, txt = pcall(list_indication, id)
    if ok and type(txt) == "string" then return txt end
    return ""
end

local function gai_poll(t)
    if not gai_open() then return end
    local txt = gai_read(gai_id)
    local shown = txt:find(GAI_MARKER, 1, true) ~= nil
    if shown and not gai_confirmed then
        gai_confirmed = true
        gai_note("found", "George interface found on indicator " .. gai_id)
    end
    -- Not confirmed yet (wheel never opened, or the indicator moved): scan every 2 s.
    if not shown and not gai_confirmed and t >= gai_next_scan then
        gai_next_scan = t + 2.0
        for id = 0, 60 do
            if id ~= gai_id then
                local other = gai_read(id)
                if other:find(GAI_MARKER, 1, true) then
                    gai_id, txt, shown, gai_confirmed = id, other, true, true
                    gai_note("found", "George interface found on indicator " .. id)
                    break
                end
            end
        end
    end
    local payload = shown and ("GAI 1\n" .. txt) or "GAI 0\n"
    if payload ~= gai_last_sent or t - gai_last_send_t >= 0.5 then
        local n, err = gai_udp:send(payload)
        if n then
            gai_last_sent, gai_last_send_t = payload, t
            gai_note("sent", "first packet sent")
            if shown then gai_note("sentshown", "first packet with the wheel shown sent") end
        else
            gai_note("senderr", "send failed: " .. tostring(err))
        end
    end
end

function LuaExportActivityNextEvent(t)
    local t_next = t + GAI_PERIOD
    if gai_prev_next then
        local ok, r = pcall(gai_prev_next, t)
        if ok and type(r) == "number" and r < t_next then t_next = r end
    end
    if t >= gai_next_poll then
        gai_next_poll = t + GAI_PERIOD
        local ok, err = pcall(gai_poll, t)
        if not ok then gai_note("pollerr", "poll error: " .. tostring(err)) end
    end
    return t_next
end
