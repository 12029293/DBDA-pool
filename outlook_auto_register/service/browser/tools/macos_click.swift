#!/usr/bin/env swift
import AppKit
import Foundation

guard CommandLine.arguments.count >= 3,
      let x = Double(CommandLine.arguments[1]),
      let y = Double(CommandLine.arguments[2]),
      let source = CGEventSource(stateID: .hidSystemState),
      let mouseDown = CGEvent(
        mouseEventSource: source,
        mouseType: .leftMouseDown,
        mouseCursorPosition: CGPoint(x: x, y: y),
        mouseButton: .left
      ),
      let mouseUp = CGEvent(
        mouseEventSource: source,
        mouseType: .leftMouseUp,
        mouseCursorPosition: CGPoint(x: x, y: y),
        mouseButton: .left
      ) else {
    fputs("usage: macos_click.swift <absolute-x> <absolute-y>\n", stderr)
    exit(2)
}
mouseDown.post(tap: .cghidEventTap)
usleep(30_000)
mouseUp.post(tap: .cghidEventTap)
