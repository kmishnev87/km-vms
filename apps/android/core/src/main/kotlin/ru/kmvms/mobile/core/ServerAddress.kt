package ru.kmvms.mobile.core

import java.net.URI
import java.net.IDN

/** An installation address only. User credentials never belong in a URL. */
class ServerAddress private constructor(val origin: URI, val base: URI) {
    fun endpoint(relative: String): URI {
        val supplied = URI(relative)
        require(!supplied.isAbsolute && supplied.rawAuthority == null && supplied.rawFragment == null && !supplied.path.startsWith('/'))
        require(supplied.path.split('/').none { it == "." || it == ".." })
        val result = base.resolve(relative)
        require(result.scheme == base.scheme && result.rawAuthority == base.rawAuthority)
        return result
    }
    override fun toString(): String = base.toASCIIString()
    companion object {
        fun parse(input: String): ServerAddress {
            val value = input.trim()
            require(value.isNotBlank() && value.length <= 2048) { "invalid_server_address" }
            val raw = URI(if (value.contains("://")) value else "https://$value")
            require(raw.scheme in setOf("http", "https") && !raw.rawAuthority.isNullOrBlank()) { "invalid_server_address" }
            require(raw.rawUserInfo == null && '@' !in raw.rawAuthority) { "invalid_server_address" }
            val uri = if (raw.host != null) raw else {
                // Java URI needs IDNA conversion to recognize a Unicode DNS hostname.
                val authority = raw.rawAuthority
                require(authority.count { it == ':' } <= 1) { "invalid_server_address" }
                val host = authority.substringBefore(':')
                val suffix = authority.substringAfter(':', "").let { if (':' in authority) ":$it" else "" }
                val ascii = IDN.toASCII(host, IDN.USE_STD3_ASCII_RULES)
                URI("${raw.scheme}://$ascii$suffix${raw.rawPath.orEmpty()}" +
                    (raw.rawQuery?.let { "?$it" } ?: "") + (raw.rawFragment?.let { "#$it" } ?: ""))
            }
            require(uri.scheme in setOf("http", "https") && !uri.host.isNullOrBlank()) { "invalid_server_address" }
            require(uri.userInfo == null && uri.rawQuery == null && uri.rawFragment == null) { "invalid_server_address" }
            require(uri.port == -1 || uri.port in 1..65535) { "invalid_server_address" }
            require(uri.path.orEmpty().split('/').none { it == "." || it == ".." }) { "invalid_server_address" }
            val origin = URI(uri.scheme, null, uri.host.lowercase(), uri.port, null, null, null)
            val canonical = uri.toASCIIString().trimEnd('/') + "/"
            return ServerAddress(origin, URI(canonical))
        }
    }
}
