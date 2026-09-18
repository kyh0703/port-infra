import Foundation
import LocalAuthentication
import Security

private enum HelperCategory: Int32 {
    case ok = 0
    case usage = 2
    case keychain = 3
    case invalidKey = 4
    case existingItemInvalid = 5
    case output = 6
}

private struct HelperFailure: Error {
    let category: HelperCategory
    let osStatus: OSStatus?

    init(_ category: HelperCategory, osStatus: OSStatus? = nil) {
        self.category = category
        self.osStatus = osStatus
    }
}

private struct KeychainIdentifier {
    static let defaultService = "com.port.infra.openbao.autounseal"
    static let defaultAccount = "port-macos-keychain-v1"

    let service: String
    let account: String
}

private enum Command {
    case initialize
    case check
    case emit
}

private struct Options {
    let command: Command
    let identifier: KeychainIdentifier
}

private func writeStatus(_ failure: HelperFailure, to handle: FileHandle) {
    let suffix = failure.osStatus.map { ":\($0)" } ?? ""
    let bytes = Array("\(failure.category.rawValue)\(suffix)\n".utf8)
    try? handle.write(contentsOf: Data(bytes))
}

private func writeSuccess(to handle: FileHandle) {
    try? handle.write(contentsOf: Data("0\n".utf8))
}

private func parseOptions(_ arguments: ArraySlice<String>) -> Result<Options, HelperFailure> {
    guard let commandName = arguments.first else {
        return .failure(HelperFailure(.usage))
    }

    let command: Command
    switch commandName {
    case "init":
        command = .initialize
    case "check":
        command = .check
    case "emit":
        command = .emit
    default:
        return .failure(HelperFailure(.usage))
    }

    var service = KeychainIdentifier.defaultService
    var account = KeychainIdentifier.defaultAccount
    var iterator = arguments.dropFirst().makeIterator()

    while let argument = iterator.next() {
        switch argument {
        case "--service":
            guard let value = iterator.next(), !value.isEmpty, !value.hasPrefix("--") else {
                return .failure(HelperFailure(.usage))
            }
            service = value
        case "--account":
            guard let value = iterator.next(), !value.isEmpty, !value.hasPrefix("--") else {
                return .failure(HelperFailure(.usage))
            }
            account = value
        default:
            return .failure(HelperFailure(.usage))
        }
    }

    guard !service.isEmpty, !account.isEmpty else {
        return .failure(HelperFailure(.usage))
    }

    return .success(
        Options(
            command: command,
            identifier: KeychainIdentifier(service: service, account: account)
        )
    )
}

private func executablePath() -> String? {
    let argument = CommandLine.arguments.first ?? ""
    guard !argument.isEmpty else {
        return nil
    }

    let url: URL
    if argument.hasPrefix("/") {
        url = URL(fileURLWithPath: argument)
    } else {
        url = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent(argument)
    }

    return url.resolvingSymlinksInPath().standardizedFileURL.path
}

private func trustedAccess() -> (SecAccess?, OSStatus) {
    guard let path = executablePath() else {
        return (nil, errSecParam)
    }

    var trustedApplication: SecTrustedApplication?
    let applicationStatus = path.withCString { pathPointer in
        SecTrustedApplicationCreateFromPath(pathPointer, &trustedApplication)
    }
    guard applicationStatus == errSecSuccess, let trustedApplication else {
        return (nil, applicationStatus)
    }

    let trustedApplications: CFArray = [trustedApplication] as CFArray
    var access: SecAccess?
    let accessStatus = SecAccessCreate(
        "OpenBao automatic unseal key" as CFString,
        trustedApplications,
        &access
    )
    guard accessStatus == errSecSuccess else {
        return (nil, accessStatus)
    }
    guard let access else {
        return (nil, errSecDecode)
    }
    return (access, errSecSuccess)
}

private func nonInteractiveAuthenticationContext() -> LAContext {
    let context = LAContext()
    context.interactionNotAllowed = true
    return context
}

private func query(for identifier: KeychainIdentifier, returningData: Bool) -> (OSStatus, Data?) {
    var query: [CFString: Any] = [
        kSecClass: kSecClassGenericPassword,
        kSecAttrService: identifier.service,
        kSecAttrAccount: identifier.account,
        kSecMatchLimit: kSecMatchLimitOne,
        kSecUseAuthenticationContext: nonInteractiveAuthenticationContext()
    ]
    if returningData {
        query[kSecReturnData] = true
    }

    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    guard status == errSecSuccess, returningData else {
        return (status, nil)
    }

    guard let data = result as? Data else {
        return (errSecDecode, nil)
    }
    return (status, data)
}

private func keyData(for identifier: KeychainIdentifier) -> Result<Data, HelperFailure> {
    let (status, data) = query(for: identifier, returningData: true)
    guard status == errSecSuccess else {
        return .failure(HelperFailure(.keychain, osStatus: status))
    }
    guard let data else {
        return .failure(HelperFailure(.keychain, osStatus: errSecDecode))
    }
    guard data.count == 32 else {
        var invalidData = data
        invalidData.resetBytes(in: 0..<invalidData.count)
        return .failure(HelperFailure(.existingItemInvalid))
    }
    return .success(data)
}

private func randomKey() -> Result<Data, HelperFailure> {
    var bytes = [UInt8](repeating: 0, count: 32)
    let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    guard status == errSecSuccess else {
        bytes.resetBytes(in: 0..<bytes.count)
        return .failure(HelperFailure(.keychain, osStatus: status))
    }
    let key = Data(bytes)
    bytes.resetBytes(in: 0..<bytes.count)
    return .success(key)
}

private func addKey(_ key: Data, for identifier: KeychainIdentifier) -> OSStatus {
    let (access, accessStatus) = trustedAccess()
    guard let access else {
        return accessStatus
    }

    let attributes: [CFString: Any] = [
        kSecClass: kSecClassGenericPassword,
        kSecAttrService: identifier.service,
        kSecAttrAccount: identifier.account,
        kSecValueData: key,
        kSecAttrAccessible: kSecAttrAccessibleWhenUnlocked,
        kSecAttrAccess: access,
        kSecAttrSynchronizable: false,
        kSecUseAuthenticationContext: nonInteractiveAuthenticationContext()
    ]
    return SecItemAdd(attributes as CFDictionary, nil)
}

private func initializeKey(for identifier: KeychainIdentifier) -> HelperFailure? {
    switch keyData(for: identifier) {
    case .success:
        return nil
    case .failure(let failure) where failure.category == .existingItemInvalid:
        return failure
    case .failure:
        break
    }

    let (status, _) = query(for: identifier, returningData: false)
    guard status == errSecItemNotFound else {
        return HelperFailure(.keychain, osStatus: status)
    }
    var key: Data
    switch randomKey() {
    case .success(let generated):
        key = generated
    case .failure(let failure):
        return failure
    }
    defer { key.resetBytes(in: 0..<key.count) }

    let addStatus = addKey(key, for: identifier)
    if addStatus == errSecSuccess {
        return nil
    }
    if addStatus == errSecDuplicateItem {
        switch keyData(for: identifier) {
        case .success:
            return nil
        case .failure(let failure):
            return failure
        }
    }
    return HelperFailure(.keychain, osStatus: addStatus)
}

private func emitKey(for identifier: KeychainIdentifier) -> HelperFailure? {
    switch keyData(for: identifier) {
    case .failure(let status):
        return status
    case .success(var key):
        defer { key.resetBytes(in: 0..<key.count) }
        do {
            try FileHandle.standardOutput.write(contentsOf: key)
            return nil
        } catch {
            return HelperFailure(.output)
        }
    }
}

private func run(_ options: Options) -> HelperFailure? {
    switch options.command {
    case .initialize:
        let failure = initializeKey(for: options.identifier)
        if let failure {
            writeStatus(failure, to: FileHandle.standardError)
        } else {
            writeSuccess(to: FileHandle.standardOutput)
        }
        return failure
    case .check:
        let failure: HelperFailure?
        switch keyData(for: options.identifier) {
        case .success:
            failure = nil
        case .failure(let keychainFailure):
            failure = keychainFailure
        }
        if let failure {
            writeStatus(failure, to: FileHandle.standardError)
        } else {
            writeSuccess(to: FileHandle.standardOutput)
        }
        return failure
    case .emit:
        let failure = emitKey(for: options.identifier)
        if let failure {
            writeStatus(failure, to: FileHandle.standardError)
        }
        return failure
    }
}

private let failure: HelperFailure?
switch parseOptions(CommandLine.arguments.dropFirst()) {
case .success(let options):
    failure = run(options)
case .failure(let parseFailure):
    writeStatus(parseFailure, to: FileHandle.standardError)
    failure = parseFailure
}
exit(failure?.category.rawValue ?? HelperCategory.ok.rawValue)
