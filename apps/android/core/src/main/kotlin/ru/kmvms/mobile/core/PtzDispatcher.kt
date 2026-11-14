package ru.kmvms.mobile.core

import kotlinx.coroutines.*
import java.util.ArrayDeque
import java.util.UUID

data class PtzCommand(val action: String, val direction: String? = null, val speed: Double? = null,
                      val controlId: String? = null, val sequence: Int? = null,
                      val preset: String? = null, val coordinates: List<Double> = emptyList())
data class PtzResult(val executed: Boolean, val stopped: Boolean = false, val ignored: Boolean = false,
                     val continuous: Boolean = false, val leaseSeconds: Double = 1.5)

/** Main-thread confined. Stop can overtake a slow start; subsequent taps wait for its acknowledgement. */
class PtzDispatcher(private val scope: CoroutineScope, private val send: suspend (PtzCommand) -> PtzResult,
                    private val clock: () -> Long = { System.nanoTime() / 1_000_000 },
                    private val makeId: () -> String = { UUID.randomUUID().toString() },
                    private val changed: (String?) -> Unit = {}, private val failed: () -> Unit = {}) {
    private class Gesture(val action: String, val direction: String, val speed: Double, val id: String) {
        var sequence = 0
        var renewal: Job? = null
        var stopAcknowledged = false
        var stopFailed = false
    }
    private class Step(val action: String, val direction: String, val speed: Double, val pressedAt: Long) {
        var released = false; var short = false; var readyAt: Long? = null; var finishing = false
        var pulse: Job? = null
        var gesture: Gesture? = null
    }
    private var disposed = false
    private var discretePending = false
    private var active: Gesture? = null
    private var current: Step? = null
    private var pressed: Step? = null
    private val steps = ArrayDeque<Step>()
    private fun owns(gesture: Gesture) = !disposed && active === gesture
    private fun owns(step: Step) = !disposed && current === step
    private fun clearSteps() { current?.pulse?.cancel(); steps.clear(); current = null; pressed = null }

    fun press(action: String, direction: String, speed: Double): Boolean {
        require(action in setOf("move", "zoom") && speed in 0.001..1.0)
        if (disposed || discretePending || pressed != null) return false
        if (steps.size >= 32) { failed(); return false }
        val step = Step(action, direction, speed, clock())
        pressed = step; steps.addLast(step); next()
        return true
    }
    fun release() {
        val step = pressed ?: return
        pressed = null; step.released = true
        step.short = current !== step || clock() - step.pressedAt <= 250
        finishReleased(step)
    }
    private fun next() {
        if (disposed || discretePending || current != null || steps.isEmpty()) return
        val step = steps.removeFirst()
        val gesture = Gesture(step.action, step.direction, step.speed, makeId())
        current = step; step.gesture = gesture; active = gesture; changed(gesture.direction)
        scope.launch {
            val result = tick(gesture)
            if (!owns(step)) return@launch
            if (result?.executed != true || result.ignored) { clearSteps(); return@launch }
            step.readyAt = clock(); finishReleased(step)
        }
    }
    private suspend fun tick(gesture: Gesture): PtzResult? {
        if (!owns(gesture)) return null
        return try {
            val result = send(PtzCommand(gesture.action, gesture.direction, gesture.speed, gesture.id, ++gesture.sequence))
            if (!owns(gesture)) {
                if (!gesture.stopAcknowledged) sendStop(gesture)
                return result
            }
            if (result.ignored) { active = null; changed(null); return result }
            if (!result.executed) throw IllegalStateException("PTZ command not executed")
            val renewalDelay = if (result.continuous) (result.leaseSeconds * 400).toLong().coerceIn(150, 600) else 300L
            gesture.renewal = scope.launch { delay(renewalDelay); tick(gesture) }
            result
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) {
            if (owns(gesture)) { stop(); if (!disposed) failed() }
            null
        }
    }
    private fun finishReleased(step: Step) {
        if (!owns(step) || !step.released || step.finishing || step.pulse != null) return
        if (step.short && step.readyAt == null) return
        val remaining = if (step.short) (100 - (clock() - step.readyAt!!)).coerceAtLeast(0) else 0
        step.pulse = scope.launch { if (remaining > 0) delay(remaining); finish(step) }
    }
    private suspend fun finish(step: Step) {
        if (!owns(step) || step.finishing) return
        step.finishing = true
        val stopped = stopGesture()
        if (!owns(step)) return
        current = null
        if (!stopped) { clearSteps(); if (step.gesture?.stopFailed != true) failed(); return }
        next()
    }
    private suspend fun sendStop(gesture: Gesture?): Boolean = try {
        val result = send(PtzCommand("stop", controlId = gesture?.id, sequence = gesture?.let { ++it.sequence }))
        gesture?.stopAcknowledged = true
        result.stopped || result.ignored
    } catch (cancelled: CancellationException) { throw cancelled }
    catch (_: Exception) { gesture?.stopFailed = true; if (!disposed) failed(); false }

    private suspend fun stopGesture(force: Boolean = false): Boolean {
        val gesture = active
        active = null
        val renewal = gesture?.renewal
        if (renewal != currentCoroutineContext()[Job]) renewal?.cancel()
        gesture?.renewal = null
        changed(null)
        if (gesture == null && !force) return true
        return sendStop(gesture)
    }
    suspend fun stop(force: Boolean = false): Boolean { clearSteps(); return stopGesture(force) }
    suspend fun dispose(): Boolean { disposed = true; clearSteps(); return stopGesture() }

    suspend fun preset(token: String): Boolean = if (token.isBlank()) false else discrete(PtzCommand("goto_preset", preset = token))
    suspend fun point(x: Double, y: Double): Boolean = if (listOf(x, y).all { it.isFinite() && it in 0.0..1.0 })
        discrete(PtzCommand("point", coordinates = listOf(x, y))) else false
    suspend fun area(left: Double, top: Double, right: Double, bottom: Double): Boolean {
        if (!listOf(left, top, right, bottom).all { it.isFinite() && it in 0.0..1.0 } || right-left < 1.0/8192 || bottom-top < 1.0/8192) return false
        return discrete(PtzCommand("area_zoom", coordinates = listOf(left, top, right, bottom)), tracked = true)
    }
    private suspend fun discrete(command: PtzCommand, tracked: Boolean = false): Boolean {
        if (disposed || discretePending) return false
        discretePending = true
        var gesture: Gesture? = null
        return try {
            clearSteps()
            if (!stopGesture() || disposed) return false
            val payload = if (tracked) {
                Gesture(command.action, "", 0.0, makeId()).also { gesture = it; it.sequence = 1; active = it }
                command.copy(controlId = gesture!!.id, sequence = 1)
            } else command
            val result = send(payload)
            if (gesture != null && !owns(gesture!!)) {
                if (!gesture!!.stopAcknowledged) sendStop(gesture)
                false
            } else result.executed && !disposed
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { if (gesture != null && owns(gesture!!)) stopGesture(); if (!disposed) failed(); false }
        finally { discretePending = false }
    }
}
