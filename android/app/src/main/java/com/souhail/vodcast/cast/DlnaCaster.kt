package com.souhail.vodcast.cast

import android.content.Context
import android.net.wifi.WifiManager
import com.souhail.vodcast.util.AppLog
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.w3c.dom.Element
import org.xml.sax.InputSource
import java.io.StringReader
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.URI
import java.util.concurrent.TimeUnit
import javax.xml.parsers.DocumentBuilderFactory

data class DlnaDevice(
    val name: String,
    val ip: String,
    val location: String,
    val controlUrl: String,
    val serviceType: String,
)

class DlnaCaster(private val context: Context) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    suspend fun discover(timeoutMs: Int = 3000): List<DlnaDevice> = withContext(Dispatchers.IO) {
        AppLog.i("DLNA", "SSDP discovery start (timeout=${timeoutMs}ms)")
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val lock = wifi.createMulticastLock("vodcast-ssdp").apply {
            setReferenceCounted(false)
            acquire()
        }
        try {
            val locations = mutableSetOf<String>()
            DatagramSocket().use { socket ->
                socket.broadcast = true
                socket.soTimeout = 700
                val message = (
                    "M-SEARCH * HTTP/1.1\r\n" +
                        "HOST: 239.255.255.250:1900\r\n" +
                        "MAN: \"ssdp:discover\"\r\n" +
                        "MX: 2\r\n" +
                        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
                    ).toByteArray()
                val target = InetAddress.getByName("239.255.255.250")
                repeat(3) { socket.send(DatagramPacket(message, message.size, target, 1900)) }
                val deadline = System.currentTimeMillis() + timeoutMs
                val buffer = ByteArray(8192)
                while (System.currentTimeMillis() < deadline) {
                    try {
                        val packet = DatagramPacket(buffer, buffer.size)
                        socket.receive(packet)
                        val text = String(packet.data, 0, packet.length)
                        Regex("(?i)^location:\\s*(.+)$", RegexOption.MULTILINE)
                            .find(text)
                            ?.groupValues
                            ?.get(1)
                            ?.trim()
                            ?.let { locations.add(it) }
                    } catch (_: java.net.SocketTimeoutException) {
                    }
                }
            }
            val devices = locations.mapNotNull {
                runCatching { describe(it) }.onFailure { err ->
                    AppLog.w("DLNA", "describe failed for $it: ${err.message}")
                }.getOrNull()
            }.distinctBy { it.controlUrl }
            devices.forEach { AppLog.i("DLNA", "found ${it.name} ${it.ip} -> ${it.controlUrl}") }
            AppLog.i("DLNA", "discovery done: ${devices.size} renderer(s)")
            devices
        } finally {
            if (lock.isHeld) lock.release()
        }
    }

    private fun describe(location: String): DlnaDevice {
        val xml = client.newCall(Request.Builder().url(location).build()).execute().use {
            it.body?.string() ?: error("empty description")
        }
        val factory = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
        val doc = factory.newDocumentBuilder().parse(InputSource(StringReader(xml)))
        val name = doc.getElementsByTagName("friendlyName").item(0)?.textContent ?: "DLNA TV"
        val services = doc.getElementsByTagName("service")
        var control: String? = null
        var serviceType: String? = null
        for (i in 0 until services.length) {
            val service = services.item(i) as? Element ?: continue
            val type = service.getElementsByTagName("serviceType").item(0)?.textContent ?: continue
            if (type.contains("AVTransport")) {
                serviceType = type
                control = service.getElementsByTagName("controlURL").item(0)?.textContent
                break
            }
        }
        val controlPath = control ?: error("no AVTransport service")
        val controlUrl = URI(location).resolve(controlPath).toString()
        val ip = URI(location).host ?: error("no host in description URL")
        return DlnaDevice(
            name = name,
            ip = ip,
            location = location,
            controlUrl = controlUrl,
            serviceType = serviceType ?: "urn:schemas-upnp-org:service:AVTransport:1",
        )
    }

    suspend fun cast(device: DlnaDevice, mediaUrl: String, title: String, mime: String) =
        withContext(Dispatchers.IO) {
            AppLog.action("dlna_cast", "tv=${device.name} mime=$mime url=${AppLog.redact(mediaUrl)}")
            val metadata = didl(title, mediaUrl, mime)
            soap(
                device,
                "SetAVTransportURI",
                "<InstanceID>0</InstanceID><CurrentURI>${escape(mediaUrl)}</CurrentURI>" +
                    "<CurrentURIMetaData>${escape(metadata)}</CurrentURIMetaData>",
            )
            soap(device, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        }

    suspend fun stop(device: DlnaDevice) = withContext(Dispatchers.IO) {
        AppLog.action("dlna_stop", "tv=${device.name}")
        soap(device, "Stop", "<InstanceID>0</InstanceID>")
    }

    private fun soap(device: DlnaDevice, action: String, inner: String): String {
        val body = """
            <?xml version="1.0" encoding="utf-8"?>
            <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
            <s:Body><u:$action xmlns:u="${device.serviceType}">$inner</u:$action></s:Body></s:Envelope>
        """.trimIndent()
        val request = Request.Builder()
            .url(device.controlUrl)
            .header("SOAPACTION", "\"${device.serviceType}#$action\"")
            .header("Content-Type", "text/xml; charset=\"utf-8\"")
            .post(body.toRequestBody("text/xml; charset=utf-8".toMediaType()))
            .build()
        client.newCall(request).execute().use { response ->
            val text = response.body?.string() ?: ""
            AppLog.i("DLNA", "$action ${device.name} -> HTTP ${response.code}")
            if (!response.isSuccessful) {
                AppLog.e("DLNA", "$action failed on ${device.name}: ${text.take(300)}")
                error("DLNA $action HTTP ${response.code}: ${text.take(200)}")
            }
            return text
        }
    }

    private fun didl(title: String, url: String, mime: String): String = """
        <DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
            xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
            <item id="1" parentID="0" restricted="1">
                <dc:title>${escape(title)}</dc:title>
                <upnp:class>object.item.videoItem</upnp:class>
                <res protocolInfo="http-get:*:$mime:DLNA.ORG_OP=01">${escape(url)}</res>
            </item>
        </DIDL-Lite>
    """.trimIndent()

    private fun escape(value: String): String = value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&apos;")
}
