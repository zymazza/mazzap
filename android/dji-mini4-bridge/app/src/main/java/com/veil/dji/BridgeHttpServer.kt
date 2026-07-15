package com.veil.dji

import android.content.Context
import android.util.Log
import dji.sdk.keyvalue.key.RemoteControllerKey
import dji.sdk.keyvalue.value.flightcontroller.FailsafeAction
import dji.v5.common.error.IDJIError
import dji.v5.et.action
import dji.v5.et.create
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.security.SecureRandom
import java.util.concurrent.Executors

class BridgeHttpServer(
    context: Context,
    private val controller: FlightController,
    private val perceptionConfig: PerceptionConfigController,
    private val cameraGimbal: CameraGimbalHttpApi,
    private val port: Int = 8765
) {
    private val executor = Executors.newCachedThreadPool()
    private var socket: ServerSocket? = null
    val token: String = context.getSharedPreferences("bridge", Context.MODE_PRIVATE)
        .let { prefs ->
            prefs.getString("token", null) ?: randomToken().also {
                prefs.edit().putString("token", it).apply()
            }
        }

    fun start() {
        executor.execute {
            try {
                socket = ServerSocket(port)
                Log.i("VeilDjiBridge", "HTTP bridge listening on $port")
                while (socket?.isClosed == false) {
                    socket?.accept()?.let { client -> executor.execute { handle(client) } }
                }
            } catch (error: Exception) {
                if (socket?.isClosed == false) {
                    BridgeState.lastEvent.set("http_server_failed:${error.message}")
                }
            }
        }
    }

    fun stop() {
        socket?.close()
        executor.shutdownNow()
    }

    private fun handle(client: Socket) {
        client.use { connection ->
            connection.soTimeout = 2_000
            val reader = BufferedReader(InputStreamReader(connection.getInputStream()))
            val requestLine = reader.readLine() ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0]
            val target = parts[1]
            val path = target.substringBefore('?')
            val headers = mutableMapOf<String, String>()
            while (true) {
                val line = reader.readLine() ?: break
                if (line.isEmpty()) break
                val colon = line.indexOf(':')
                if (colon > 0) headers[line.substring(0, colon).trim().lowercase()] = line.substring(colon + 1).trim()
            }

            if (method == "GET" && path == "/health") {
                respond(connection, 200, JSONObject().put("ok", true).toString())
                return
            }

            if (headers["x-veil-token"] != token) {
                respond(connection, 401, JSONObject().put("error", "unauthorized").toString())
                return
            }

            val query = try {
                parseQuery(target.substringAfter('?', ""))
            } catch (error: Exception) {
                respond(connection, 400, JSONObject().put("error", "invalid_query").toString())
                return
            }

            if (method == "GET" && path == "/status") {
                respond(connection, 200, BridgeState.toJson()
                    .put("supervisory_port", port)
                    .put("video_port", 8766)
                    .put("realtime_control_port", 8767)
                    .put("telemetry_port", 8768)
                    .toString())
                return
            }

            if (method == "GET" && path == "/perception/config") {
                respond(connection, 200, perceptionConfig.statusJson().toString())
                return
            }

            if (method == "GET" && path == "/camera-gimbal/status") {
                respond(connection, 200, cameraGimbal.statusJson().toString())
                return
            }

            if (method == "GET" && path == "/camera-gimbal/capabilities") {
                respond(connection, 200, cameraGimbal.capabilitiesJson().toString())
                return
            }

            if (method == "GET" && path == "/commands") {
                try {
                    val limit = query["limit"]?.let {
                        it.toIntOrNull()
                            ?: throw IllegalArgumentException("limit must be an integer")
                    } ?: 16
                    require(limit in 1..BridgeCommandJournal.CAPACITY) {
                        "limit must be between 1 and ${BridgeCommandJournal.CAPACITY}"
                    }
                    respond(connection, 200, BridgeCommandJournal.historyJson(limit).toString())
                } catch (error: Exception) {
                    respond(
                        connection,
                        400,
                        JSONObject().put("error", error.message ?: "bad_request").toString()
                    )
                }
                return
            }

            if (method == "GET" && path.startsWith("/commands/")) {
                val commandId = path.removePrefix("/commands/")
                val command = commandId.takeIf { it.isNotBlank() }
                    ?.let(BridgeCommandJournal.journal::get)
                if (command == null) {
                    respond(
                        connection,
                        404,
                        JSONObject()
                            .put("error", "command_not_found")
                            .put("command_id", commandId)
                            .toString()
                    )
                } else {
                    respond(connection, 200, BridgeCommandJournal.recordJson(command).toString())
                }
                return
            }

            if (method != "POST") {
                respond(connection, 404, JSONObject().put("error", "not_found").toString())
                return
            }

            try {
                val command = when (path) {
                    "/takeoff" -> {
                        require(query["confirm"] == "TAKEOFF") { "confirm=TAKEOFF is required" }
                        controller.takeoff()
                    }
                    "/land" -> controller.land()
                    "/land/confirm" -> controller.confirmLanding()
                    "/failsafe-action" -> {
                        require(query["confirm"] == "SET_FAILSAFE_ACTION") {
                            "confirm=SET_FAILSAFE_ACTION is required"
                        }
                        val action = when (query["action"]?.uppercase()) {
                            "HOVER" -> FailsafeAction.HOVER
                            "LANDING" -> FailsafeAction.LANDING
                            "GOHOME" -> FailsafeAction.GOHOME
                            else -> throw IllegalArgumentException(
                                "action must be HOVER, LANDING, or GOHOME"
                            )
                        }
                        controller.setFailsafeAction(action)
                    }
                    "/pairing/start" -> {
                        RemoteControllerKey.KeyRequestPairing.create().action({
                            BridgeState.lastEvent.set("pairing_requested")
                        }, { error: IDJIError ->
                            BridgeState.lastEvent.set("pairing_failed:$error")
                        })
                        null
                    }
                    "/pairing/stop" -> {
                        RemoteControllerKey.KeyStopPairing.create().action({
                            BridgeState.lastEvent.set("pairing_stopped")
                        }, { error: IDJIError ->
                            BridgeState.lastEvent.set("pairing_stop_failed:$error")
                        })
                        null
                    }
                    "/virtual-stick/enable" -> controller.enableVirtualStick(
                        when (query["mode"]?.lowercase() ?: "body_velocity") {
                            "body_velocity", "velocity" -> VirtualControlMode.BODY_VELOCITY
                            "sticks" -> VirtualControlMode.STICKS
                            else -> throw IllegalArgumentException(
                                "mode must be body_velocity or sticks"
                            )
                        }
                    )
                    "/virtual-stick/disable" -> controller.disableVirtualStick()
                    "/virtual-stick" -> controller.submitSticksObserved(
                        query.int("lh"), query.int("lv"), query.int("rh"), query.int("rv")
                    )
                    "/virtual-stick/velocity" -> controller.submitBodyVelocityObserved(
                        query.double("forward_mps"),
                        query.double("right_mps"),
                        query.double("up_mps"),
                        query.double("yaw_rate_deg_s")
                    )
                    "/perception/config" -> {
                        require(query["confirm"] == "SET_PERCEPTION_CONFIG") {
                            "confirm=SET_PERCEPTION_CONFIG is required"
                        }
                        perceptionConfig.set(query["setting"], query["value"])
                    }
                    "/camera-gimbal/command" -> cameraGimbal.submit(query["action"], query)
                    else -> {
                        respond(connection, 404, JSONObject().put("error", "not_found").toString())
                        return
                    }
                }
                val response = JSONObject().put("path", path)
                if (command != null) {
                    val selection = CommandHttpResponsePolicy.select(command.state)
                    response
                        .put("request_recorded", true)
                        .put("command_id", command.id)
                        .put("state", command.state.wireName)
                        .put("result_url", "/commands/${command.id}")
                        .put("command", BridgeCommandJournal.recordJson(command))
                    selection.acceptedForProcessing?.let {
                        response.put("accepted_for_processing", it)
                    }
                    respond(connection, selection.statusCode, response.toString())
                } else {
                    // Pairing actions predate the command journal and remain asynchronous.
                    response.put("accepted_for_processing", true)
                    respond(connection, 202, response.toString())
                }
            } catch (error: Exception) {
                respond(connection, 400, JSONObject().put("error", error.message ?: "bad_request").toString())
            }
        }
    }

    private fun respond(socket: Socket, status: Int, body: String) {
        val reason = when (status) {
            200 -> "OK"
            202 -> "Accepted"
            409 -> "Conflict"
            400 -> "Bad Request"
            401 -> "Unauthorized"
            404 -> "Not Found"
            else -> "Error"
        }
        val bytes = body.toByteArray(Charsets.UTF_8)
        socket.getOutputStream().apply {
            write("HTTP/1.1 $status $reason\r\n".toByteArray())
            write("Content-Type: application/json\r\n".toByteArray())
            write("Content-Length: ${bytes.size}\r\n".toByteArray())
            write("Connection: close\r\n\r\n".toByteArray())
            write(bytes)
            flush()
        }
    }

    private fun parseQuery(query: String): Map<String, String> = query
        .split('&')
        .filter { it.isNotBlank() }
        .associate {
            val parts = it.split('=', limit = 2)
            URLDecoder.decode(parts[0], "UTF-8") to URLDecoder.decode(parts.getOrElse(1) { "" }, "UTF-8")
        }

    private fun Map<String, String>.int(name: String): Int =
        get(name)?.toIntOrNull() ?: throw IllegalArgumentException("missing integer: $name")

    private fun Map<String, String>.double(name: String): Double =
        get(name)?.toDoubleOrNull()?.takeIf { it.isFinite() }
            ?: throw IllegalArgumentException("missing finite number: $name")

    private fun randomToken(): String {
        val bytes = ByteArray(24)
        SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }
}
