---@diagnostic disable: undefined-global
LUAGUI_NAME = "kh1_native_test"
LUAGUI_AUTH = "test"
LUAGUI_DESC = "Debug overlay driver for kh1_native_debug.dll -- press F6 in-game to toggle the window"

local kh1_lib = nil
local kh1_debug = nil

function _OnInit()
	if GAME_ID == 0xAF71841E and ENGINE_TYPE == "BACKEND" then
		require("VersionCheck")
		kh1_lib = require("kh1_lua_library")
		kh1_debug = require("kh1_native_debug")
		ConsolePrint("kh1_native_debug overlay loaded - press F6 to toggle")
	else
		ConsolePrint("KH1 not detected, not running script")
	end
end

-- The overlay window runs on its own thread and can't safely call game
-- functions itself (see kh1_native_debug's dllmain.cpp) -- it only queues a
-- named request, which we dispatch here through the real Lua functions every
-- frame (from kh1_lua_library, backed by kh1_native.dll), since this runs on
-- the same thread the rest of this library's calls do.
function _OnFrame()
	if kh1_debug == nil then return end

	-- Drives any pending open_text_box(..., duration_seconds) auto-closes --
	-- must run every frame regardless of whether a debug action is pending,
	-- so it's called before the early-return below.
	kh1_lib.update_text_boxes()

	-- Also drives the F6 show/hide toggle -- must be polled every frame
	-- regardless of whether an action is pending.
	local action = kh1_debug.poll_debug_action()
	if action == nil then return end

	if action.action == "spawn_prize" then
		local ok = kh1_lib.spawn_prize(action.param1)
		kh1_debug.set_debug_result("spawn_prize(" .. action.param1 .. ") = " .. tostring(ok))
	elseif action.action == "show_custom_popup" then
		local ok = kh1_lib.show_custom_item_popup(action.param_text)
		kh1_debug.set_debug_result("show_custom_item_popup(\"" .. action.param_text .. "\") = " .. tostring(ok))
	elseif action.action == "open_text_box" then
		-- nums = {duration, style, x, y, width, height} -- see kh1_native_debug's
		-- dllmain.cpp DrawForm, which now queues every text-box field in one
		-- action instead of needing separate Set Style/Position/Size steps
		-- before Open Text Box.
		local duration, style, x, y, width, height =
			action.nums[1], math.floor(action.nums[2]), math.floor(action.nums[3]),
			math.floor(action.nums[4]), math.floor(action.nums[5]), math.floor(action.nums[6])
		local ok = kh1_lib.open_text_box(action.param_text, action.param1, duration, style, x, y, width, height)
		kh1_debug.set_debug_result("open_text_box(\"" .. action.param_text .. "\", id=" .. action.param1 ..
			", dur=" .. duration .. ", style=" .. style .. ", pos=" .. x .. "," .. y ..
			", size=" .. width .. "x" .. height .. ") = " .. tostring(ok))
	elseif action.action == "close_text_box" then
		local ok = kh1_lib.close_text_box(action.param1)
		kh1_debug.set_debug_result("close_text_box(" .. action.param1 .. ") = " .. tostring(ok))
	elseif action.action == "play_se2" then
		local se_id, param_2 = math.floor(action.nums[1]), math.floor(action.nums[2])
		local ok = kh1_lib.play_se2(se_id, param_2)
		kh1_debug.set_debug_result("play_se2(" .. se_id .. ", " .. param_2 .. ") = " .. tostring(ok))
	end
end
