-- Installed by install.py, which inserts quoted, absolute paths below.
local PYTHON = @PYTHON@
local APP = @APP@
local DATA = @DATA@
local CONFIG = @CONFIG@
local dialog, path_box, primary_menu, reference_menu, references_label, status_label, model_box
local primary_button, refresh_button, file_button, settings_button
local source_note, retry_button
local mode_box
local references = {}
local choices, job, job_media, job_action, loaded = {}, nil, nil, nil, false
local sequence = 0
local choices_media = nil
local extra_row = 13
local recover_job

function descriptor()
    return {
        title = "Codex Russian Subtitles",
        version = "1.6.2",
        author = "VLC Codex Subtitles",
        shortdesc = "Russian subtitles from multiple languages",
        description = "Translate text, PGS or DVD subtitles through Codex CLI, with local OCR and multilingual references.",
        capabilities = {"input-listener"}
    }
end

local function shell_quote(value)
    return "'" .. string.gsub(value, "'", "'\\''") .. "'"
end

local function json_string(value)
    local escaped = string.gsub(value or "", '[%z\1-\31\\"]', function(c)
        if c == '"' then return '\\"' end
        if c == '\\' then return '\\\\' end
        return string.format('\\u%04x', string.byte(c))
    end)
    return '"' .. escaped .. '"'
end

local function decode(value)
    return (string.gsub(value or "", "%%(%x%x)", function(h) return string.char(tonumber(h, 16)) end))
end

local function encode(value)
    return (string.gsub(value or "", "([^A-Za-z0-9%-._~])", function(c) return string.format("%%%02X", string.byte(c)) end))
end

-- VLC 3 macOS parses labels as Latin-1 HTML. Encode every Unicode code point
-- as an entity; otherwise an em dash or Cyrillic text can make the label vanish.
local function html(value)
    local out, column = {}, 0
    for c in string.gmatch(value or "", "[%z\1-\127\194-\244][\128-\191]*") do
        local b = string.byte(c)
        local code = b
        if #c == 2 then code = (b - 192) * 64 + string.byte(c, 2) - 128
        elseif #c == 3 then code = (b - 224) * 4096 + (string.byte(c, 2) - 128) * 64 + string.byte(c, 3) - 128
        elseif #c == 4 then code = (b - 240) * 262144 + (string.byte(c, 2) - 128) * 4096 + (string.byte(c, 3) - 128) * 64 + string.byte(c, 4) - 128 end
        if c == "\n" then table.insert(out, "<br/>") column = 0
        elseif c == "&" then table.insert(out, "&amp;")
        elseif c == "<" then table.insert(out, "&lt;")
        elseif c == ">" then table.insert(out, "&gt;")
        elseif code > 127 then table.insert(out, "&#" .. tostring(code) .. ";")
        else table.insert(out, c) end
        column = column + 1
        if (column >= 72 and c == " ") or column >= 88 then
            table.insert(out, "<br/>") column = 0
        end
    end
    return table.concat(out)
end

local function status(value)
    if status_label then status_label:set_text(html(value)) end
end

local function update_references()
    local names = {}
    for _, source in ipairs(references) do table.insert(names, source.label) end
    references_label:set_text(html(#names == 0 and "None added. The main source is enough to begin." or
        ("Added " .. #names .. ": " .. table.concat(names, "; "))))
end

local function media_uri()
    local item = vlc.input.item()
    local uri = item and item:uri() or ""
    if string.match(uri, "^dvdnav:") or string.match(uri, "^dvd:") then
        -- VLC reserves title 0 for the DVD menu; real DVD titles are 1-based.
        local ok, title = pcall(function() return vlc.var.get(vlc.object.input(), "title") end)
        if ok and type(title) == "number" and title > 0 then
            uri = string.gsub(uri, "#.*$", "") .. "#codex-title=" .. math.floor(title)
        end
    end
    return uri
end

local function read_lines(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local lines = {}
    for line in f:lines() do table.insert(lines, decode(line)) end
    f:close()
    return lines
end

local function media_key(uri)
    local value = 5381
    for i = 1, #uri do value = (value * 33 + string.byte(uri, i)) % 4294967296 end
    return string.format("%08x", value)
end

local function busy()
    if not job then return false end
    local lines = read_lines(job .. "/status.txt")
    return not lines or (lines[2] ~= "done" and lines[2] ~= "discovered" and lines[2] ~= "error" and lines[2] ~= "cancelled")
end

local function source_json(source)
    return '{"kind":' .. json_string(source.kind) .. ',"value":' .. json_string(source.value) ..
        ',"label":' .. json_string(source.label) .. '}'
end

local function add_choice(source)
    for _, existing in ipairs(choices) do
        if existing.kind == source.kind and existing.value == source.value then return end
    end
    if #choices == 0 then
        primary_menu:clear()
        reference_menu:clear()
        reference_menu:add_value("None - choose only if you want an extra source", 0)
    end
    table.insert(choices, source)
    local id = #choices
    primary_menu:add_value(source.label, id)
    reference_menu:add_value(source.label, id)
end

local function load_choices(folder)
    local f = io.open(folder .. "/choices.tsv", "r")
    if not f then return false end
    primary_menu:clear()
    reference_menu:clear()
    choices, references = {}, {}
    for row in f:lines() do
        local kind, value, label = string.match(row, "([^\t]+)\t([^\t]+)\t(.*)")
        if kind then add_choice({kind=decode(kind), value=decode(value), label=decode(label)}) end
    end
    f:close()
    if #choices == 0 then
        primary_menu:add_value("No supported subtitles found", 0)
        reference_menu:add_value("None - no extra source", 0)
    end
    update_references()
    return true
end

local function launch(action, extra)
    if busy() then status("A job is running. Refresh progress or cancel it first.") return end
    local created = false
    for attempt = 1, 100 do
        sequence = sequence + 1
        job = DATA .. "/jobs/" .. tostring(os.time()) .. "-" .. tostring(sequence)
        if vlc.io.mkdir(job, "0700") == 0 then created = true break end
    end
    if not created then status("Could not create a job folder. Run the installer again.") job = nil return end
    job_media, job_action, loaded = media_uri(), action, false
    local f, err = io.open(job .. "/request.json", "w")
    if not f then status("Could not write job: " .. tostring(err)) job = nil return end
    f:write('{"action":' .. json_string(action) .. ',"media":' .. json_string(job_media) .. (extra or "") .. '}')
    f:close()
    if action == "translate" then
        local snapshot = io.open(job .. "/choices.tsv", "w")
        if snapshot then
            local primary = choices[primary_menu:get_value()] or choices[1]
            local function save(source)
                snapshot:write(encode(source.kind), "\t", encode(source.value), "\t", encode(source.label), "\n")
            end
            save(primary)
            for _, source in ipairs(choices) do if source ~= primary then save(source) end end
            snapshot:close()
        end
    end
    local command = "cd " .. shell_quote(APP) .. " && " .. shell_quote(PYTHON) ..
        " -m codex_subs start-job " .. shell_quote(job) .. " --config " .. shell_quote(CONFIG) ..
        " --wait " .. (action == "discover" and "1.2" or "0") ..
        " > " .. shell_quote(job .. "/launcher.log") .. " 2>&1 < /dev/null"
    local pointer = io.open(DATA .. "/latest-job.txt", "w")
    if pointer then pointer:write(string.match(job, "([^/]+)$")) pointer:close() end
    local launched = os.execute(command)
    if launched == nil or launched == false or (type(launched) == "number" and launched ~= 0) then
        status("Could not start the helper. Details: " .. job .. "/launcher.log")
        job = nil
        return
    end
    if action == "translate" and job_media ~= "" then
        local saved = io.open(DATA .. "/video-" .. media_key(job_media) .. ".txt", "w")
        if saved then
            saved:write(string.match(job, "([^/]+)$"), "\n", encode(job_media), "\n")
            saved:close()
        end
    end
    status(action == "discover" and "Finding subtitle sources..." or
        "Starting translation. Playback can continue. Use Refresh progress for the latest status.")
    -- VLC 3 net.poll has no timeout: never call it as a timer.
    -- The short-lived launcher detaches translation and returns immediately.
    check_progress(false)
end

function scan_sources()
    if media_uri() == "" then status("Open a video in VLC, or use Add a file to supply text subtitles.") return end
    launch("discover")
end

function add_file()
    local path = path_box:get_text()
    if path == "" then status("Paste the full path of a subtitle file first.") return end
    local f = io.open(path, "rb")
    if not f then status("Cannot read that path. Paste an absolute path without surrounding quotes.") return end
    f:close()
    add_choice({kind="file", value=path, label=string.match(path, "([^/]+)$") or path})
    path_box:set_text("")
    status("Subtitle file added. Select it in step 1 to translate it, or add it as a reference in step 2.")
end

function add_reference()
    local source = choices[reference_menu:get_value()]
    if not source then status("Choose a supporting subtitle source first.") return end
    for _, old in ipairs(references) do
        if old.kind == source.kind and old.value == source.value then status("That reference is already added.") return end
    end
    table.insert(references, source)
    update_references()
    status("Reference added. Add another, or create the Russian subtitles below.")
end

function clear_references()
    references = {}
    update_references()
end

function translate_subtitles()
    local primary = choices[primary_menu:get_value()] or choices[1]
    if not primary then status("No supported source is available. Add a subtitle file in step 1 to continue.") return end
    local supporting = {}
    for _, source in ipairs(references) do
        if source.kind ~= primary.kind or source.value ~= primary.value then table.insert(supporting, source_json(source)) end
    end
    launch("translate", ',"primary":' .. source_json(primary) .. ',"references":[' ..
        table.concat(supporting, ",") .. '],"model":' .. json_string(model_box and model_box:get_text() or "") ..
        ',"profile":' .. json_string(mode_box and mode_box:get_value() == 2 and "careful" or "light"))
end

function check_progress(autoload)
    if autoload == nil then autoload = true end
    if not job then status("Open a video or add a subtitle file, then choose Create Russian subtitles.") return end
    local cancelled = io.open(job .. "/cancel", "r")
    if cancelled then
        cancelled:close()
        os.execute("cd " .. shell_quote(APP) .. " && " .. shell_quote(PYTHON) ..
            " -m codex_subs cancel-job " .. shell_quote(job) .. " >/dev/null 2>&1")
    end
    local lines = read_lines(job .. "/status.txt")
    if not lines then
        status("Waiting for the background helper. Press Refresh progress in a moment. If it stays here, inspect: " .. job .. "/worker.log")
        return
    end
    if lines[1] ~= "1" then status("Unsupported helper response. Reinstall the extension.") return end
    if cancelled and busy() then
        if primary_button then primary_button:set_text("Cancelling translation...") end
        status("Cancellation requested. Waiting for the background helper to stop. Refresh progress in a moment.")
        return
    end
    if lines[2] == "discovered" and not loaded then
        if job_media ~= media_uri() then status("The video changed. Scan the current video again.") return end
        choices_media = job_media
        load_choices(job)
        loaded = true
    end
    if primary_button then primary_button:set_text(busy() and job_action == "translate" and "Translation is running" or "Create Russian subtitles") end
    if refresh_button then refresh_button:set_text(lines[2] == "done" and not loaded and "Load Russian subtitles" or "Refresh progress") end
    if lines[2] == "discovered" then
        source_note:set_text(html("Detected " .. #choices .. " subtitle sources from this video and its folder."))
        if #choices == 0 then
            if string.find(lines[5] or "", "unsupported", 1, true) or string.find(lines[5] or "", "requires macOS", 1, true) then
                status(lines[5])
            else
                status("No text subtitles found. Use Add subtitle file to supply an SRT, ASS or WebVTT file.")
            end
        else
            local has_pgs = false
            for _, source in ipairs(choices) do
                if string.find(source.label, "(OCR)", 1, true) or string.match(source.value, "%.sup$") then has_pgs = true end
            end
            status(has_pgs and "Ready. Image subtitles are read locally with OCR before translation. Choose Create Russian subtitles." or
                "Ready to translate. Choose Create Russian subtitles.")
        end
        return
    end
    if lines[2] == "done" then
        if autoload and not loaded and job_media ~= "" and job_media == media_uri() then
            local ok, err = pcall(vlc.input.add_subtitle, lines[6], true)
            if not ok then status("Saved: " .. lines[6] .. ". VLC could not load it: " .. tostring(err)) return end
            loaded = true
            refresh_button:set_text("Refresh progress")
            status("Complete. Russian subtitles are loaded. " ..
                (string.find(lines[5] or "", "untranslated", 1, true) and (lines[5] .. " ") or "") .. "Saved: " .. lines[6])
        else
            status((string.find(lines[5] or "", "untranslated", 1, true) and (lines[5] .. " ") or "") ..
                "Russian subtitles ready: " .. lines[6] .. (loaded and " (loaded)" or " — open with Subtitle > Add Subtitle File."))
        end
    elseif lines[2] == "error" then
        if job_action == "discover" and not retry_button then
            retry_button = dialog:add_button("Retry detection", scan_sources, 1, extra_row)
            extra_row = extra_row + 1
        end
        status("Could not finish: " .. (lines[5] or "Unknown error") .. " Retry after fixing the issue.")
    elseif lines[2] == "repairing" then
        status((lines[5] or "Checking the original source...") .. " Playback can continue.")
    elseif lines[2] == "ocr" then
        status("Reading subtitle images: " .. (lines[3] or "0") .. " of " .. (lines[4] or "0") .. ". " .. (lines[5] or "") ..
            " Refresh progress for the latest status.")
    elseif lines[2] == "extracting" then
        status((lines[5] or "Extracting subtitle images...") .. " Playback can continue.")
    elseif lines[2] == "cancelled" then
        status("Cancelled. Reviewed chunks are saved; Create Russian subtitles will resume them.")
    else
        status((lines[5] or "Working") .. "\n" ..
            (lines[3] or "0") .. " of " .. (lines[4] or "0") .. " cues reviewed.\nRefresh progress for the latest status; playback can continue.")
    end
end

function cancel_job()
    if not busy() then status("No job is running.") return end
    local f = io.open(job .. "/cancel", "w")
    if not f then status("Could not request cancellation: job folder is not writable.") return end
    f:write("cancel\n") f:close()
    status("Cancelling translation...")
    check_progress(false)
end

function input_changed()
    if media_uri() == choices_media then return end
    choices_media = media_uri()
    if primary_menu then
        primary_menu:clear()
        reference_menu:clear()
        choices, references = {}, {}
        update_references()
        -- Detach the dialog from the previous video, without cancelling its worker.
        job, job_media, job_action, loaded = nil, nil, nil, false
        if media_uri() ~= "" then
            if not recover_job() then scan_sources() end
        else status("Open a video, or add a subtitle file.") end
    end
end

recover_job = function()
    local uri = media_uri()
    if uri == "" then return false end
    local function restore(name, indexed)
        if not name or not string.match(name, "^%d+%-%d+$") then return false end
        local candidate = DATA .. "/jobs/" .. name
        local lines = read_lines(candidate .. "/status.txt")
        if lines and lines[7] == uri then
            if lines[2] == "discovered" then
                local version = read_lines(candidate .. "/discovery-version.txt")
                if not version or version[1] ~= "1.3" then return false end
            end
            job, job_media, loaded = candidate, lines[7], false
            job_action = lines[2] == "discovered" and "discover" or "translate"
            if job_action == "translate" then
                load_choices(job)
                source_note:set_text("Showing the source used by the saved translation job.")
            end
            check_progress(true)
            return true
        elseif not lines and indexed then
            local request = io.open(candidate .. "/request.json", "r")
            if request then
                request:close()
                job, job_media, job_action, loaded = candidate, uri, "translate", false
                load_choices(job)
                check_progress(false)
                return true
            end
        end
        return false
    end
    local saved = read_lines(DATA .. "/video-" .. media_key(uri) .. ".txt")
    if saved and saved[2] == uri and restore(saved[1], true) then return true end
    local pointer = read_lines(DATA .. "/latest-job.txt")
    local names, seen = {}, {}
    if pointer and pointer[1] and string.match(pointer[1], "^%d+%-%d+$") then
        table.insert(names, pointer[1]) seen[pointer[1]] = true
    end
    if vlc.io.readdir then
        for _, name in ipairs(vlc.io.readdir(DATA .. "/jobs") or {}) do
            if string.match(name, "^%d+%-%d+$") and not seen[name] then table.insert(names, name) seen[name] = true end
        end
    end
    table.sort(names, function(a, b)
        local at, aq = string.match(a, "^(%d+)%-(%d+)$")
        local bt, bq = string.match(b, "^(%d+)%-(%d+)$")
        return tonumber(at) > tonumber(bt) or (tonumber(at) == tonumber(bt) and tonumber(aq) > tonumber(bq))
    end)
    -- A later discovery must not hide an already saved translation.
    for pass = 1, 2 do
        for _, name in ipairs(names) do
            local folder = DATA .. "/jobs/" .. name
            local lines = read_lines(folder .. "/status.txt")
            if lines and lines[7] == uri and ((pass == 1) == (lines[2] ~= "discovered")) then
                if restore(name, false) then return true end
            end
        end
    end
    return false
end

function show_file_input()
    if path_box then status("Paste the subtitle's full path in the field at the bottom, then click Add file.") return end
    dialog:add_label("Full path to an external subtitle file:", 1, extra_row, 2, 1)
    path_box = dialog:add_text_input("", 1, extra_row + 1)
    dialog:add_button("Add file", add_file, 2, extra_row + 1)
    extra_row = extra_row + 2
    file_button:set_text("Add file path below")
    status("Paste the full subtitle file path in the field at the bottom, then click Add file.")
end

function show_settings()
    if model_box then return end
    dialog:add_label("Model override (blank uses your configured model):", 1, extra_row, 2, 1)
    model_box = dialog:add_text_input("", 1, extra_row + 1, 2, 1)
    mode_box = dialog:add_dropdown(1, extra_row + 2, 2, 1)
    mode_box:add_value("Light - low reasoning, parallel batches", 1)
    mode_box:add_value("Careful - medium reasoning, smaller batches", 2)
    extra_row = extra_row + 3
    settings_button:set_text("Model settings shown below")
end

function activate()
    local function guarded(fn)
        return function()
            local ok, err = pcall(fn)
            if not ok then status("Extension error: " .. tostring(err)) end
        end
    end
    dialog = vlc.dialog("Codex Russian Subtitles")
    choices, references, path_box, model_box, retry_button, mode_box = {}, {}, nil, nil, nil, nil
    choices_media = media_uri()
    extra_row = 13
    -- Single-row widgets only. VLC 3 macOS's stretching list breaks the grid.
    dialog:add_label("<b>1. Subtitles to translate</b>", 1, 1, 2, 1)
    primary_menu = dialog:add_dropdown(1, 2, 2, 1)
    primary_menu:add_value("Detecting subtitles from the current video...", 0)
    source_note = dialog:add_label("Sources are detected automatically.", 1, 3)
    file_button = dialog:add_button("Add subtitle file...", guarded(show_file_input), 2, 3)

    dialog:add_label("<b>2. Improve with other translations</b> (optional)", 1, 4, 2, 1)
    reference_menu = dialog:add_dropdown(1, 5)
    reference_menu:add_value("None - choose only if you want an extra source", 0)
    dialog:add_button("Add reference", guarded(add_reference), 2, 5)
    references_label = dialog:add_label("No extra sources added.", 1, 6)
    dialog:add_button("Clear references", guarded(clear_references), 2, 6)

    dialog:add_label("<b>3. Create the Russian subtitles</b>", 1, 7, 2, 1)
    primary_button = dialog:add_button("Create Russian subtitles", guarded(translate_subtitles), 1, 8, 2, 1)
    dialog:add_label("<small>Fidelity review included. Text and flagged subtitle images use your Codex account.</small>", 1, 9, 2, 1)
    status_label = dialog:add_label("Choose the main subtitles above to get started.", 1, 10, 2, 1)
    refresh_button = dialog:add_button("Refresh progress", guarded(check_progress), 1, 11)
    dialog:add_button("Cancel translation", guarded(cancel_job), 2, 11)
    settings_button = dialog:add_button("Model settings...", guarded(show_settings), 2, 12)
    dialog:show()
    if not recover_job() then
        if media_uri() ~= "" then scan_sources()
        else
            primary_menu:clear()
            primary_menu:add_value("Open a video, or add a subtitle file", 0)
            source_note:set_text("Open a video to detect its subtitle tracks.")
        end
    end
end

function deactivate()
    if dialog then dialog:delete() dialog = nil end
    primary_menu, reference_menu, references_label, status_label = nil, nil, nil, nil
end

function close()
    vlc.deactivate()
end
