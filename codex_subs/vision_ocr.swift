// Local OCR. No network, language correction, or generative reconstruction.
import Foundation
import Vision

struct Input: Decodable { let id: String; let path: String; let language: String }
struct Line: Codable { let text: String; let confidence: Float; let x: Double; let y: Double; let width: Double; let height: Double }
struct Output: Codable { let id: String; let lines: [Line]; let error: String? }

func recognize(_ input: Input) -> Output {
    do {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.01
        let supported = try request.supportedRecognitionLanguages()
        if !input.language.isEmpty {
            guard let language = supported.first(where: { $0 == input.language || $0.hasPrefix(input.language + "-") }) else {
                return Output(id: input.id, lines: [], error: "The local OCR engine does not support language \(input.language).")
            }
            request.recognitionLanguages = [language]
        } else {
            if #available(macOS 13, *) { request.automaticallyDetectsLanguage = true }
        }
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: input.path), options: [:])
        try handler.perform([request])
        let lines = (request.results ?? []).compactMap { observation -> Line? in
            guard let best = observation.topCandidates(1).first else { return nil }
            // Vision can extend a box a fraction beyond the image edge.
            let b = observation.boundingBox.intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
            return Line(text: best.string, confidence: best.confidence, x: b.minX, y: 1-b.maxY, width: b.width, height: b.height)
        }.sorted { a,b in abs(a.y-b.y) < min(a.height,b.height)*0.5 ? a.x < b.x : a.y < b.y }
        return Output(id: input.id, lines: lines, error: nil)
    } catch { return Output(id: input.id, lines: [], error: error.localizedDescription) }
}

do {
    let inputs = try JSONDecoder().decode([Input].self, from: FileHandle.standardInput.readDataToEndOfFile())
    for input in inputs {
        let output = autoreleasepool { recognize(input) }
        FileHandle.standardOutput.write(try JSONEncoder().encode(output))
        FileHandle.standardOutput.write(Data([10]))
    }
} catch {
    FileHandle.standardError.write(Data("ERROR: \(error.localizedDescription)\n".utf8))
    exit(1)
}
