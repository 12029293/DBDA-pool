#!/usr/bin/env swift
import AppKit
import Foundation

let data = FileHandle.standardInput.readDataToEndOfFile()
guard let text = String(data: data, encoding: .utf8) else {
    fputs("stdin is not valid UTF-8\n", stderr)
    exit(2)
}
if text.isEmpty {
    exit(0)
}

guard let source = CGEventSource(stateID: .hidSystemState) else {
    fputs("cannot create CGEvent keyboard source\n", stderr)
    exit(3)
}

// Chromium can silently discard a single keyboard event carrying a long
// Unicode payload.  Emit one Unicode keyDown/keyUp pair per UTF-16 unit so the
// web page receives the same input event stream as ordinary typing, without
// involving the current IME or the global clipboard.
for unit in text.utf16 {
    guard let keyDown = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
          let keyUp = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
        fputs("cannot create CGEvent keyboard event\n", stderr)
        exit(4)
    }
    var character = unit
    keyDown.keyboardSetUnicodeString(stringLength: 1, unicodeString: &character)
    keyUp.keyboardSetUnicodeString(stringLength: 1, unicodeString: &character)
    keyDown.post(tap: .cghidEventTap)
    usleep(7_000)
    keyUp.post(tap: .cghidEventTap)
    usleep(5_000)
}
