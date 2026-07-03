from app.core.configs import initialize_configuration, get_config

manager = initialize_configuration()          # env -> paths -> load -> validate -> cache
log_level = get_config("logging.level", "INFO")
if manager.is_feature_enabled("fg2_brain"):
    ...                                        # start the AI Brain
