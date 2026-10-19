package com.souhail.vodcast.util

import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.content.FileProvider
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors

object AppLog {
    private const val TAG = "VODCAST"
    private const val MAX_BYTES = 1_000_000L
    private const val MAX_LINES = 700

    private val formatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss.SSS")
    private val executor = Executors.newSingleThreadExecutor()
    private val lock = Any()

    @Volatile
    private var logDir: File? = null

    @Volatile
    private var remoteBase: String? = null

    @Volatile
    private var remoteToken: String? = null

    fun setRemote(baseUrl: String, token: String) {
        remoteBase = baseUrl.trimEnd('/')
        remoteToken = token
    }

    fun init(context: Context) {
        logDir = File(context.filesDir, "logs").apply { mkdirs() }
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            appendSync("E", "CRASH", "uncaught on ${thread.name}\n${throwable.stackTraceToString()}")
            previous?.uncaughtException(thread, throwable)
        }
        val version = runCatching {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        }.getOrNull() ?: "?"
        i(
            "APP",
            "start version=$version sdk=${Build.VERSION.SDK_INT} " +
                "device=${Build.MANUFACTURER} ${Build.MODEL}",
        )
    }

    fun d(tag: String, message: String) = write("D", tag, message)
    fun i(tag: String, message: String) = write("I", tag, message)
    fun w(tag: String, message: String) = write("W", tag, message)

    fun e(tag: String, message: String, throwable: Throwable? = null) =
        write("E", tag, if (throwable == null) message else "$message\n${throwable.stackTraceToString()}")

    fun action(name: String, details: String = "") =
        i("ACTION", if (details.isBlank()) name else "$name $details")

    fun notify(message: String) = i("NOTIFY", message)

    fun api(
        method: String,
        path: String,
        code: Int,
        millis: Long,
        bytes: Long = 0,
        error: String? = null,
    ) {
        val line = "$method $path -> $code ${millis}ms ${bytes}B" + (error?.let { " error=$it" } ?: "")
        if (code >= 400 || error != null) w("API", line) else i("API", line)
    }

    fun redact(text: String): String = text
        .replace(Regex("([?&]sig=)[a-fA-F0-9]+"), "$1***")
        .replace(Regex("([?&]exp=)[0-9]+"), "$1***")
        .replace(Regex("([?&]token=)[A-Za-z0-9_\\-]+"), "$1***")
        .replace(Regex("([?&]password=)[^&\\s]+"), "$1***")
        .replace(Regex("(?i)(authorization: bearer )\\S+"), "$1***")

    fun recent(maxLines: Int = MAX_LINES): List<String> {
        val dir = logDir ?: return emptyList()
        val file = File(dir, "vodcast.log")
        if (!file.exists()) return emptyList()
        return runCatching { file.readLines().takeLast(maxLines) }.getOrDefault(emptyList())
    }

    fun clear() {
        val dir = logDir ?: return
        synchronized(lock) {
            File(dir, "vodcast.log").delete()
            File(dir, "vodcast.log.1").delete()
        }
        i("LOG", "log cleared")
    }

    fun share(context: Context) {
        val dir = logDir ?: return
        val file = File(dir, "vodcast.log")
        if (!file.exists()) {
            i("LOG", "nothing to share")
            return
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, "VOD Cast logs")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(intent, "Share VOD Cast logs"))
    }

    private fun write(level: String, tag: String, message: String) {
        val line = "${LocalDateTime.now().format(formatter)} $level/$tag ${redact(message)}"
        when (level) {
            "E" -> Log.e(TAG, "$tag $message")
            "W" -> Log.w(TAG, "$tag $message")
            "D" -> Log.d(TAG, "$tag $message")
            else -> Log.i(TAG, "$tag $message")
        }
        val dir = logDir
        if (dir != null) executor.execute { append(dir, line) }
        if (level == "E") executor.execute { postRemote(line) }
    }

    private fun postRemote(line: String) {
        val base = remoteBase ?: return
        val token = remoteToken ?: return
        try {
            val message = URLEncoder.encode(line.takeLast(1800), "UTF-8")
            val url = URL("$base/api/app/client-log?level=E&tag=APP&message=$message")
            val connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.setRequestProperty("Authorization", "Bearer $token")
            connection.connectTimeout = 4000
            connection.readTimeout = 4000
            connection.responseCode
            connection.disconnect()
        } catch (_: Exception) {
        }
    }

    private fun appendSync(level: String, tag: String, message: String) {
        val line = "${LocalDateTime.now().format(formatter)} $level/$tag ${redact(message)}"
        Log.e(TAG, "$tag $message")
        postRemote(line)
        val dir = logDir ?: return
        append(dir, line)
    }

    private fun append(dir: File, line: String) {
        synchronized(lock) {
            try {
                val file = File(dir, "vodcast.log")
                if (file.exists() && file.length() > MAX_BYTES) {
                    val old = File(dir, "vodcast.log.1")
                    if (old.exists()) old.delete()
                    file.renameTo(old)
                }
                file.appendText(line + "\n")
            } catch (_: Exception) {
            }
        }
    }
}
