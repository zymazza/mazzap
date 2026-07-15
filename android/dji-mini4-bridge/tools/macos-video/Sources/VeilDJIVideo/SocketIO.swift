import Darwin
import Foundation

/// Shared bounded TCP setup for both viewer streams.  A dead Wi-Fi route must
/// not strand either reconnect worker in connect(2), and a reset peer must not
/// turn the small authentication write into a process-wide SIGPIPE.
enum ViewerSocket {
    static func connect(
        host: String,
        port: UInt16,
        streamName: String,
        receiveBufferBytes: Int32? = nil
    ) throws -> Int32 {
        var hints = addrinfo(
            ai_flags: AI_ADDRCONFIG,
            ai_family: AF_UNSPEC,
            ai_socktype: SOCK_STREAM,
            ai_protocol: IPPROTO_TCP,
            ai_addrlen: 0,
            ai_canonname: nil,
            ai_addr: nil,
            ai_next: nil
        )
        var firstResult: UnsafeMutablePointer<addrinfo>?
        let lookup = getaddrinfo(host, String(port), &hints, &firstResult)
        guard lookup == 0, let firstResult else {
            throw ViewerError.connection(
                "\(streamName) host lookup failed: \(String(cString: gai_strerror(lookup)))"
            )
        }
        defer { freeaddrinfo(firstResult) }

        var candidate: UnsafeMutablePointer<addrinfo>? = firstResult
        var lastError = ECONNREFUSED
        while let info = candidate?.pointee {
            let socket = Darwin.socket(info.ai_family, info.ai_socktype, info.ai_protocol)
            if socket >= 0 {
                do {
                    try configure(
                        socket,
                        streamName: streamName,
                        receiveBufferBytes: receiveBufferBytes
                    )
                    try boundedConnect(
                        socket,
                        address: info.ai_addr,
                        addressLength: info.ai_addrlen,
                        streamName: streamName
                    )
                    return socket
                } catch {
                    lastError = errno
                    Darwin.close(socket)
                    if candidate?.pointee.ai_next == nil { throw error }
                }
            } else {
                lastError = errno
            }
            candidate = info.ai_next
        }
        throw ViewerError.connection(
            "\(streamName) connection failed: \(errorText(lastError))"
        )
    }

    static func sendAll(
        _ socket: Int32,
        data: Data,
        streamName: String
    ) throws {
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < raw.count {
                let count = Darwin.send(socket, base.advanced(by: sent), raw.count - sent, 0)
                if count > 0 {
                    sent += count
                } else if count < 0 && errno == EINTR {
                    continue
                } else if count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) {
                    throw ViewerError.connection("\(streamName) authentication send timed out")
                } else {
                    let code = count < 0 ? errno : ECONNRESET
                    throw ViewerError.connection(
                        "\(streamName) authentication failed: \(errorText(code))"
                    )
                }
            }
        }
    }

    static func errorText(_ code: Int32) -> String {
        String(cString: strerror(code))
    }

    private static func configure(
        _ socket: Int32,
        streamName: String,
        receiveBufferBytes: Int32?
    ) throws {
        var one: Int32 = 1
        guard setsockopt(
            socket, IPPROTO_TCP, TCP_NODELAY, &one,
            socklen_t(MemoryLayout.size(ofValue: one))
        ) == 0,
        setsockopt(
            socket, SOL_SOCKET, SO_NOSIGPIPE, &one,
            socklen_t(MemoryLayout.size(ofValue: one))
        ) == 0 else {
            throw ViewerError.connection(
                "\(streamName) socket setup failed: \(errorText(errno))"
            )
        }
        if var receiveBufferBytes {
            guard setsockopt(
                socket, SOL_SOCKET, SO_RCVBUF, &receiveBufferBytes,
                socklen_t(MemoryLayout.size(ofValue: receiveBufferBytes))
            ) == 0 else {
                throw ViewerError.connection(
                    "\(streamName) receive-buffer setup failed: \(errorText(errno))"
                )
            }
        }
        var receiveTimeout = timeval(tv_sec: 0, tv_usec: 250_000)
        var sendTimeout = timeval(tv_sec: 0, tv_usec: 500_000)
        guard setsockopt(
            socket, SOL_SOCKET, SO_RCVTIMEO, &receiveTimeout,
            socklen_t(MemoryLayout.size(ofValue: receiveTimeout))
        ) == 0,
        setsockopt(
            socket, SOL_SOCKET, SO_SNDTIMEO, &sendTimeout,
            socklen_t(MemoryLayout.size(ofValue: sendTimeout))
        ) == 0 else {
            throw ViewerError.connection(
                "\(streamName) socket timeout setup failed: \(errorText(errno))"
            )
        }
    }

    private static func boundedConnect(
        _ socket: Int32,
        address: UnsafePointer<sockaddr>?,
        addressLength: socklen_t,
        streamName: String
    ) throws {
        let originalFlags = fcntl(socket, F_GETFL, 0)
        guard originalFlags >= 0,
              fcntl(socket, F_SETFL, originalFlags | O_NONBLOCK) == 0 else {
            throw ViewerError.connection(
                "\(streamName) nonblocking setup failed: \(errorText(errno))"
            )
        }

        let result = Darwin.connect(socket, address, addressLength)
        if result != 0 {
            guard errno == EINPROGRESS || errno == EINTR else {
                throw ViewerError.connection(
                    "\(streamName) connection failed: \(errorText(errno))"
                )
            }
            var descriptor = pollfd(fd: socket, events: Int16(POLLOUT), revents: 0)
            let startedAt = DispatchTime.now().uptimeNanoseconds
            var pollResult: Int32 = -1
            repeat {
                let elapsedMS = Int(
                    (DispatchTime.now().uptimeNanoseconds - startedAt) / 1_000_000
                )
                let remainingMS = max(0, connectTimeoutMilliseconds - elapsedMS)
                guard remainingMS > 0 else {
                    throw ViewerError.connection("\(streamName) connection timed out")
                }
                pollResult = Darwin.poll(&descriptor, 1, Int32(remainingMS))
            } while pollResult < 0 && errno == EINTR
            guard pollResult > 0 else {
                if pollResult == 0 {
                    throw ViewerError.connection("\(streamName) connection timed out")
                }
                throw ViewerError.connection(
                    "\(streamName) connection poll failed: \(errorText(errno))"
                )
            }
            var socketError: Int32 = 0
            var errorLength = socklen_t(MemoryLayout.size(ofValue: socketError))
            guard getsockopt(
                socket, SOL_SOCKET, SO_ERROR, &socketError, &errorLength
            ) == 0, socketError == 0 else {
                let code = socketError == 0 ? errno : socketError
                throw ViewerError.connection(
                    "\(streamName) connection failed: \(errorText(code))"
                )
            }
        }
        guard fcntl(socket, F_SETFL, originalFlags) == 0 else {
            throw ViewerError.connection(
                "\(streamName) blocking-mode restore failed: \(errorText(errno))"
            )
        }
    }

    private static let connectTimeoutMilliseconds = 1_500
}
