from pet.bridge.intent_smoother import AnimationSmoother, Priority


def test_emotion_respects_four_second_lock_and_alert_can_interrupt():
    controller = AnimationSmoother()
    assert controller.request(Priority.EMOTION, 0, 1)
    assert not controller.request(Priority.EMOTION, 3.99, 1)
    assert controller.request(Priority.SPEAKING, 3.99, 1)
    assert controller.request(Priority.HIGH_ALERT, 4, 1)
    assert not controller.request(Priority.SPEAKING, 4.1, 1)
    assert controller.expire(8)
    assert controller.request(Priority.EMOTION, 8, 1)
