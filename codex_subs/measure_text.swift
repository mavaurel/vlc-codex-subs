import Foundation
import AppKit
import CoreText
let data = FileHandle.standardInput.readDataToEndOfFile()
do {
    let words = try JSONDecoder().decode([String].self, from: data)
    let font = NSFont(name: "Arial", size: 100) ?? NSFont.systemFont(ofSize: 100)
    let widths = words.map { word -> Double in
        let text = NSAttributedString(string: word, attributes: [.font: font])
        return CTLineGetTypographicBounds(CTLineCreateWithAttributedString(text), nil, nil, nil)
    }
    FileHandle.standardOutput.write(try JSONEncoder().encode(widths))
} catch {
    FileHandle.standardError.write(Data("ERROR: \(error)\n".utf8)); exit(1)
}
