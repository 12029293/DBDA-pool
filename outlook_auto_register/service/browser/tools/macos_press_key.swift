#!/usr/bin/env swift
import AppKit
import Foundation

guard CommandLine.arguments.count >= 2,
      let rawCode = UInt16(CommandLine.arguments[1]),
      let source = CGEventSource(stateID: .hidSystemState),
      let keyDown = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(rawCode), keyDown: true),
      let keyUp = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(rawCode), keyDown: false) else {
    fputs("usage: macos_press_key.swift <key-code> [--command]\n", stderr)
    exit(2)
}
if CommandLine.arguments.contains("--command") {
    guard let commandDown = CGEvent(keyboardEventSource: source, virtualKey: 55, keyDown: true),
          let commandUp = CGEvent(keyboardEventSource: source, virtualKey: 55, keyDown: false) else {
        fputs("cannot create Command modifier events\n", stderr)
        exit(3)
    }
    commandDown.flags = .maskCommand
    keyDown.flags = .maskCommand
    keyUp.flags = .maskCommand
    commandUp.flags = []
    commandDown.post(tap: .cghidEventTap)
    usleep(15_000)
    keyDown.post(tap: .cghidEventTap)
    usleep(25_000)
    keyUp.post(tap: .cghidEventTap)
    usleep(15_000)
    commandUp.post(tap: .cghidEventTap)
} else {
    keyDown.post(tap: .cghidEventTap)
    usleep(25_000)
    keyUp.post(tap: .cghidEventTap)
}
