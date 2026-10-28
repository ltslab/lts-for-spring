package sample;

import org.springframework.batch.core.Job;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.security.config.web.server.ServerHttpSecurity;
import reactor.core.publisher.Mono;
import reactor.netty.http.server.HttpServer;

/** Touches every LTS for Spring project so that each one is on the compile classpath. */
@SpringBootApplication
public class App {

	static final Class<?>[] USED = { Job.class, ServerHttpSecurity.class, Mono.class, HttpServer.class };

	public static void main(String[] args) {
		SpringApplication.run(App.class, args);
	}

}
