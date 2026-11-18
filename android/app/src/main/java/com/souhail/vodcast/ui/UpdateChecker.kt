package com.souhail.vodcast.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings as AndroidSettings
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.FileProvider
import com.souhail.vodcast.data.Api
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.data.UpdateInfo
import com.souhail.vodcast.util.AppLog
import kotlinx.coroutines.launch
import java.io.File

private const val CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000L

fun appVersionCode(context: Context): Int = runCatching {
    context.packageManager.getPackageInfo(context.packageName, 0).let {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) it.longVersionCode.toInt() else it.versionCode
    }
}.getOrDefault(0)

fun appVersionName(context: Context): String = runCatching {
    context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: ""
}.getOrDefault("")

private fun versionParts(value: String): List<Int> =
    Regex("\\d+").findAll(value).map { it.value.toInt() }.toList()

fun isNewerVersion(candidate: String, current: String): Boolean {
    val a = versionParts(candidate)
    val b = versionParts(current)
    for (i in 0 until maxOf(a.size, b.size)) {
        val x = a.getOrElse(i) { 0 }
        val y = b.getOrElse(i) { 0 }
        if (x != y) return x > y
    }
    return false
}

private suspend fun resolveUpdate(api: Api, currentCode: Int, currentName: String): UpdateInfo? {
    val server = runCatching { api.update(currentCode) }.getOrNull()
    if (server != null && server.available && server.version_code > currentCode) return server
    val release = runCatching { api.githubLatest() }.getOrNull() ?: return null
    val tag = release.tag_name.removePrefix("v")
    if (!isNewerVersion(tag, currentName)) return null
    val asset = release.assets.firstOrNull { it.name.startsWith("vodcast-v") && it.name.endsWith(".apk") }
        ?: release.assets.firstOrNull { it.name.endsWith(".apk") }
    AppLog.i("UPDATE", "github=$tag current=$currentName")
    return UpdateInfo(
        ok = true,
        version_name = tag,
        version_code = currentCode + 1,
        size = asset?.size ?: 0,
        notes = release.body.take(1200),
        available = true,
        apk_url = asset?.browser_download_url,
    )
}

fun installApk(context: Context, file: File) {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !context.packageManager.canRequestPackageInstalls()) {
        runCatching {
            context.startActivity(
                Intent(
                    AndroidSettings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${context.packageName}"),
                ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        }
        return
    }
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val intent = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, "application/vnd.android.package-archive")
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    }
    context.startActivity(intent)
}

@Composable
fun UpdateGate(api: Api, settings: Settings, currentVersion: Int, currentName: String,
               trigger: Int = 0, notify: (String) -> Unit = {}) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var info by remember { mutableStateOf<UpdateInfo?>(null) }
    var busy by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf(0f) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(api, trigger) {
        val forced = trigger > 0
        val now = System.currentTimeMillis()
        if (!forced && now - settings.lastUpdateCheck < CHECK_INTERVAL_MS) return@LaunchedEffect
        settings.lastUpdateCheck = now
        val found = resolveUpdate(api, currentVersion, currentName)
        if (found == null) {
            if (forced) notify("You're on the latest version (${currentName}).")
            return@LaunchedEffect
        }
        AppLog.i("UPDATE", "available ${found.version_name} (${found.version_code}) current=$currentVersion")
        if (settings.skippedVersion == found.version_code && !forced) return@LaunchedEffect
        info = found
    }

    val update = info ?: return
    AlertDialog(
        onDismissRequest = {
            if (!busy) {
                settings.skippedVersion = update.version_code
                info = null
            }
        },
        title = { Text("Update ${update.version_name}") },
        text = {
            Column {
                if (busy) {
                    LinearProgressIndicator(
                        progress = { progress },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text("Downloading… ${(progress * 100).toInt()}%", style = MaterialTheme.typography.bodySmall)
                } else {
                    Text(
                        update.notes.ifBlank { "A newer build is available on your server." },
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    val mb = update.size / (1024.0 * 1024.0)
                    Text(
                        String.format("%.1f MB · version %d", mb, update.version_code),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                error?.let {
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = !busy,
                onClick = {
                    scope.launch {
                        busy = true
                        error = null
                        progress = 0f
                        try {
                            val dir = File(context.cacheDir, "updates").apply { mkdirs() }
                            val apk = File(dir, "vodcast-${update.version_name}.apk")
                            val direct = update.apk_url
                            if (!direct.isNullOrBlank()) {
                                api.downloadUrl(direct, apk) { progress = it }
                            } else {
                                api.downloadUpdate(apk) { progress = it }
                            }
                            busy = false
                            AppLog.i("UPDATE", "downloaded ${apk.length()} bytes")
                            installApk(context, apk)
                            info = null
                        } catch (e: Exception) {
                            busy = false
                            error = e.message ?: "download failed"
                            AppLog.w("UPDATE", "download failed: ${e.message}")
                        }
                    }
                },
            ) { Text(if (busy) "Downloading…" else "Download & install") }
        },
        dismissButton = {
            TextButton(
                enabled = !busy,
                onClick = {
                    settings.skippedVersion = update.version_code
                    info = null
                },
            ) { Text("Later") }
        },
    )
}
